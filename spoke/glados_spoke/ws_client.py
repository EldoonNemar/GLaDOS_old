"""WebSocket client with automatic reconnection.

Speaker playback uses sounddevice (no aplay/sox) and supports both MP3 and
raw float32 PCM audio. The hub sends MP3 for smallest file size; the spoke
decodes it with pygame and plays via sounddevice.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import time
from typing import Any, Awaitable, Callable

import numpy as np
import sounddevice as sd
import websockets


logger = logging.getLogger(__name__)


def _alsa_hw_to_device(hw: str) -> int | None:
    """Convert ALSA hw:X,Y string to sounddevice device index.
    
    Returns None if no matching device found.
    """
    try:
        parts = hw.replace("hw:", "").split(",")
        if len(parts) != 2:
            return None
        
        card = int(parts[0])
        device = int(parts[1])
        
        # Get all devices
        for i, dev in enumerate(sd.query_devices()):
            name = dev.get("name", "")
            # Check if this device matches the ALSA hw address
            if f"hw:{card},{device}" in name or f"[hw:{card},{device}]" in name:
                logger.info("sounddevice device %d matches ALSA %s: %s", i, hw, name)
                return i
    except Exception as e:
        logger.warning("Failed to convert ALSA %s to device index: %s", hw, e)
    
    return None


def _build_url(host: str, port: int, path: str) -> str:
    return f"ws://{host}:{port}{path}"


def _decode_mp3_to_float32(mp3_data: bytes, sample_rate: int) -> np.ndarray:
    """Decode MP3 bytes to a float32 numpy array suitable for sounddevice.

    Tries pygame first (fast, native), falls back to subprocess mpg123/ffplay,
    and finally to raw int16 PCM if nothing else is available.
    """
    # Try pygame first (fastest)
    try:
        import pygame.mixer
        import pygame.sndarray

        pygame.mixer.init(frequency=sample_rate, size=-16, channels=1)
        buffer = io.BytesIO(mp3_data)
        sound = pygame.mixer.Sound(buffer)
        samples = pygame.sndarray.array(sound)
        pygame.mixer.uninit()
        return (samples.astype(np.int16) / 32768.0).astype(np.float32).flatten()
    except Exception:
        pass

    # Try mpg123 (fast, common on Linux)
    try:
        import subprocess
        proc = subprocess.Popen(
            ["mpg123", "-s", "-r", str(sample_rate), "-f", "0", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw, _ = proc.communicate(mp3_data, timeout=10)
        if raw:
            return (np.frombuffer(raw, dtype=np.int16) / 32768.0).astype(np.float32)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass

    # Try ffplay/ffmpeg
    try:
        import subprocess
        proc = subprocess.Popen(
            ["ffmpeg", "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(sample_rate), "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw, _ = proc.communicate(mp3_data, timeout=10)
        if raw:
            return (np.frombuffer(raw, dtype=np.int16) / 32768.0).astype(np.float32)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass

    # Fallback: treat as raw int16 PCM
    return (np.frombuffer(mp3_data, dtype=np.int16) / 32768.0).astype(np.float32)


def _decode_raw_float32(data: bytes, sample_rate: int) -> np.ndarray:
    """Interpret bytes as raw float32 PCM."""
    return np.frombuffer(data, dtype=np.float32)


class ReconnectingWS:
    """WebSocket connection with automatic reconnection."""

    def __init__(
        self,
        url: str,
        *,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        reconnect_jitter: float = 0.5,
        reconnect_attempts: int | None = None,
        heartbeat_interval_s: float = 5.0,
        on_open: Callable[[], Awaitable[None]] | None = None,
        on_close: Callable[[int, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self.url = url
        self.reconnect_base_delay = reconnect_base_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.reconnect_jitter = reconnect_jitter
        self.reconnect_attempts = reconnect_attempts
        self.heartbeat_interval_s = heartbeat_interval_s
        self.on_open = on_open
        self.on_close = on_close
        self._ws = None
        self._running = False
        self._task = None
        self._attempt = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        logger.info("WS[%s]: connect loop started", self.url)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("WS[%s]: stopped", self.url)

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError(f"WS[{self.url}]: not connected")
        await self._ws.send(data)

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._one_connection()
            except (ConnectionRefusedError, OSError):
                pass
            except Exception:
                logger.exception("WS[%s]: error", self.url)
            if not self._running:
                break
            await self._backoff_delay()

    async def _one_connection(self) -> None:
        """Connect to the WebSocket server and handle messages."""
        while self._running:
            try:
                ws = await websockets.connect(self.url)
                self._ws = ws
                self._attempt = 0
                logger.info("WS[%s]: connected", self.url)
                if self.on_open:
                    await self.on_open()
                try:
                    async for msg in ws:
                        if isinstance(msg, bytes):
                            await self._handle_binary(msg)
                        else:
                            await self._handle_text(msg)
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WS[%s]: connection closed", self.url)
                finally:
                    self._ws = None
            except (ConnectionRefusedError, OSError):
                logger.debug("WS[%s]: connection failed", self.url)
            await self._backoff_delay()

    async def _backoff_delay(self) -> None:
        """Exponential backoff with jitter before reconnecting."""
        self._attempt += 1
        delay = min(
            self.reconnect_base_delay * (2 ** (self._attempt - 1)),
            self.reconnect_max_delay,
        )
        jitter = random.uniform(0, delay * self.reconnect_jitter)
        total = delay + jitter
        logger.info("WS[%s]: reconnecting in %.1fs (attempt %d)", self.url, total, self._attempt)
        await asyncio.sleep(total)

    async def _handle_binary(self, data: bytes) -> None:
        pass

    async def _handle_text(self, text: str) -> None:
        pass


class MicClient(ReconnectingWS):
    """Microphone client: pushes PCM audio chunks to the hub."""

    def __init__(
        self,
        url: str,
        on_capture: Callable[[bytes], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(url, **kwargs)
        self._on_capture = on_capture
        self._out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)

    async def _handle_text(self, text: str) -> None:
        logger.debug("MicClient received: %s", text[:80])

    async def _handle_binary(self, data: bytes) -> None:
        raise NotImplementedError("MicClient does not expect binary frames")

    def push_sample(self, samples: np.ndarray) -> None:
        """Bridge from sounddevice callback into async send loop.

        Thread-safe: uses Queue.put_nowait() which is safe to call from
        the sounddevice callback thread.
        """
        if self._ws is None or self._ws.state.name != "OPEN":
            return
        data = np.ascontiguousarray(samples, dtype=np.float32).tobytes()
        if self._out.full():
            self._out.get_nowait()  # discard oldest to bound latency
            self._out.task_done()
        self._out.put_nowait(data)

    async def _safe_send(self, data: bytes) -> None:
        if self._ws is not None and self._ws.state.name == "OPEN":
            try:
                await self._ws.send(data)
            except Exception:
                pass

    async def _connect_loop(self) -> None:
        """Start the connection loop AND the sender task."""
        self._running = True
        self._task = asyncio.create_task(self._one_connection())
        self._sender_task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self) -> None:
        """Continuously drain the queue and send to the WebSocket."""
        while True:
            try:
                data = await self._out.get()
                try:
                    if self._ws and self._ws.state.name == "OPEN":
                        await self._ws.send(data)
                finally:
                    self._out.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MicClient sender error")

    async def stop(self) -> None:
        self._running = False
        if hasattr(self, "_sender_task"):
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        await super().stop()


def _resample_audio(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio from one sample rate to another using scipy (or fallback)."""
    try:
        from scipy import signal as scipy_signal
        if from_rate == to_rate:
            return samples
        num_samples = int(len(samples) * to_rate / from_rate)
        if num_samples <= 0:
            return samples
        return scipy_signal.resample(samples, num_samples).astype(np.float32)
    except Exception:
        # Fallback: simple linear interpolation (no scipy)
        if from_rate == to_rate or len(samples) == 0:
            return samples
        ratio = to_rate / from_rate
        new_len = int(len(samples) * ratio)
        if new_len <= 0:
            return samples
        indices = np.linspace(0, len(samples) - 1, new_len)
        return np.interp(indices, np.arange(len(samples)), samples).astype(np.float32)


class SpeakerClient(ReconnectingWS):
    """Speaker client: receives TTS audio from /speaker and plays it via sounddevice.

    Accepts both MP3 and raw float32 PCM. The hub sends MP3 for smallest
    file size; the spoke decodes it with pygame and plays via sounddevice.
    """

    def __init__(
        self,
        url: str,
        spk_device: str | int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(url, **kwargs)
        # Convert ALSA hw:X,Y to sounddevice index if needed
        if isinstance(spk_device, str):
            converted = _alsa_hw_to_device(spk_device)
            if converted is not None:
                self.spk_device = converted
                logger.info("Converted ALSA %s to sounddevice index %d", spk_device, converted)
            else:
                logger.warning("Could not convert ALSA %s to sounddevice index, using as-is", spk_device)
                self.spk_device = spk_device
        else:
            self.spk_device = spk_device
        self._current_sample_rate: int = 22050
        self._current_play_time: float = 0.0
        self._current_id: str | None = None
        self._format: str = "raw"  # "mp3" or "raw"
        self._playback_lock = asyncio.Lock()
        self._stop_playback = False
        # Cached working sample rate found by probing at startup
        self._working_rate: int | None = None

    def _get_supported_rates(self) -> list[int]:
        """Return sample rates this speaker device supports, in preference order."""
        try:
            if self.spk_device is not None:
                dev = sd.query_devices(device=self.spk_device)
                native = int(round(dev.get("default_samplerate", 0)))
            else:
                dev = sd.query_devices(kind="output")
                native = int(round(dev.get("default_samplerate", 22050)))
            if native and native > 0:
                return [native, 22050, 16000, 44100]
        except Exception:
            pass
        return [22050, 16000, 44100]

    def _probe_working_rate(self) -> int | None:
        """Probe the speaker device to find a working sample rate.

        Tests each candidate rate with a short silent burst. Returns
        the first rate that successfully opens the stream, or None.
        
        The XVF3800 USB audio device requires int16 format and specific
        parameters. Uses blocksize=1024 for stability.
        """
        import numpy as np
        import sounddevice as sd  # noqa: F811

        # The XVF3800 USB audio device is picky:
        # - Requires int16 format (not float32)
        # - Needs 16000 Hz (native rate)
        # - blocksize=1024 for stability
        duration = 0.05
        for rate in self._get_supported_rates():
            try:
                silent = np.zeros(int(rate * duration), dtype=np.int16)
                stream = sd.OutputStream(
                    samplerate=rate, channels=1, blocksize=1024,
                    device=self.spk_device, latency="low",
                    dtype=np.int16,  # XVF3800 requires int16, not float32
                )
                stream.start()
                stream.write(silent)
                stream.stop()
                stream.close()
                logger.info("Speaker: probed working sample rate %d Hz (int16)", rate)
                return rate
            except Exception:
                logger.debug("Speaker: probe failed at %d Hz", rate)
                continue
        logger.warning("Speaker: no working sample rate found via probe")
        return None

    async def on_open(self) -> None:
        """Send our supported sample rates on connection."""
        # Probe for a working sample rate once at startup
        self._working_rate = self._probe_working_rate()
        if self._working_rate:
            self._current_sample_rate = self._working_rate

        rates = self._get_supported_rates()
        await self.send("capabilities:" + ",".join(str(r) for r in rates))
        logger.info("Speaker: advertised supported rates {}, working rate {}",
                    rates, self._working_rate)

    async def _play(self, data: bytes, sample_rate: int) -> None:
        """Decode and play audio via sounddevice, using cached working rate."""
        if not data:
            return

        # Use cached working rate as primary; hub rate is only a suggestion
        if self._working_rate:
            effective_rate = self._working_rate
        else:
            effective_rate = sample_rate

        # Detect format and decode
        if self._format == "mp3":
            samples = _decode_mp3_to_float32(data, effective_rate)
        else:
            samples = _decode_raw_float32(data, effective_rate)

        if samples.size == 0:
            return

        logger.info("Speaker: playing %d samples (%.2fs) via sounddevice", len(samples), len(samples) / effective_rate)

        # Respect play-time scheduling
        scheduled_time = self._current_play_time
        if scheduled_time > time.time():
            await asyncio.sleep(max(0.0, scheduled_time - time.time()))

        # Cancel on interruption
        interrupted = False
        played = False

        # Try working rate first, then other supported rates as fallback
        rates_to_try = [effective_rate] + [r for r in self._get_supported_rates()
                                            if r != effective_rate and r != sample_rate]

        for try_rate in rates_to_try:
            if try_rate != effective_rate:
                logger.info("Speaker: resampling to %d Hz (fallback)", try_rate)
                samples = _resample_audio(samples, effective_rate, try_rate)

            if self._stop_playback:
                break

            try:
                # Convert to int16 for XVF3800 USB audio device compatibility
                # The XVF3800 requires S16_LE format (16-bit integer PCM)
                int16_samples = (samples * 32767.0).astype(np.int16)
                sd.play(int16_samples, try_rate, device=self.spk_device, blocking=False, dtype=np.int16)

                # Wait for playback to finish or interruption
                play_time_s = len(int16_samples) / try_rate
                start = time.monotonic()
                while not interrupted:
                    elapsed = time.monotonic() - start
                    if elapsed >= play_time_s:
                        break
                    await asyncio.sleep(min(0.05, play_time_s - elapsed))

                sd.stop()
                played = True
                logger.info("Speaker: playback complete at %d Hz (%d samples)", try_rate, len(int16_samples))
                break
            except Exception as e:
                logger.warning("Speaker: playback failed at %d Hz: %s, trying next rate...", try_rate, e)
                try:
                    sd.stop()
                except Exception:
                    pass
                continue

        if not played:
            logger.error("Speaker: all sample rate attempts failed")

        self._stop_playback = False

    async def _handle_binary(self, data: bytes) -> None:
        if not data:
            return
        logger.info("Speaker: received %d bytes binary frame (%s)", len(data), self._format)
        async with self._playback_lock:
            try:
                self._stop_playback = False
                await self._play(data, self._current_sample_rate)
            except Exception:
                logger.exception("SpeakerClient: error scheduling audio")

    async def _handle_text(self, text: str) -> None:
        if text.startswith("time:"):
            self._current_play_time = float(text.split(":", 1)[1])
        elif text.startswith("sampleRate:"):
            self._current_sample_rate = int(text.split(":", 1)[1] or 22050)
        elif text == "reset":
            self._stop_playback = True
            sd.stop()
        elif text.startswith("format:"):
            self._format = text.split(":", 1)[1].strip()
            logger.info("Speaker: audio format set to %s", self._format)
