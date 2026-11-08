"""Local audio capture and playback for the spoke."""

from __future__ import annotations

import logging
import math
import numpy as np
from numpy.typing import NDArray

try:
    from speexdsp import EchoCanceller as _SpeexEchoCanceller
    _HAS_SPEEX = True
except ImportError:
    _HAS_SPEEX = False

try:
    import sounddevice as sd
    _HAS_SD = True
except ImportError:
    _HAS_SD = False

logger = logging.getLogger(__name__)


class SpeexAEC:
    """Acoustic echo cancellation backed by libspeexdsp.

    Uses the known playback audio (reference signal) to estimate the echo
    path and subtract it from the microphone input via frequency-domain
    adaptive filtering.

    Requires the system package ``libspeexdsp-dev`` (Debian/Ubuntu) to build.
    """

    def __init__(self, sample_rate: int = 16000, frame_size: int = 512) -> None:
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._aec = _SpeexEchoCanceller.create(frame_size, 4096, sample_rate)
        self._ref_buffer: NDArray[np.float32] = np.empty(0, dtype=np.float32)

    def update_reference(self, samples: NDArray[np.float32]) -> None:
        """Update the playback reference at the AEC sample rate."""
        if len(samples) == 0:
            self._ref_buffer = np.empty(0, dtype=np.float32)
            return
        from scipy import signal as scipy_signal
        if self.sample_rate != 16000:
            divisor = math.gcd(self.sample_rate, 16000)
            samples = scipy_signal.resample_poly(
                samples, 16000 // divisor, self.sample_rate // divisor,
            ).astype(np.float32)
        self._ref_buffer = samples

    def process(self, mic: NDArray[np.float32], ref: NDArray[np.float32]) -> NDArray[np.float32]:
        """Run AEC on a single mic frame using the reference playback audio."""
        if len(mic) == 0:
            return mic
        if len(ref) < self.frame_size:
            return mic

        mic_bytes = mic[:self.frame_size].astype(np.int16).tobytes()
        ref_bytes = ref[:self.frame_size].astype(np.int16).tobytes()

        cleaned_bytes = self._aec.process(mic_bytes, ref_bytes)
        cleaned = np.frombuffer(cleaned_bytes, dtype=np.int16).astype(np.float32)
        return cleaned


class SpokeAudio:
    """Manages local mic capture and speaker playback."""

    MIC_SAMPLE_RATE = 16000

    def __init__(
        self,
        mic_sample_rate: int = 16000,
        mic_channels: int = 1,
        mic_chunk_frames: int = 512,
        mic_device: int | str | None = None,
        mic_volume: float = 1.0,
        spk_sample_rate: int = 22050,
        spk_device: int | str | None = None,
        echo_aec_enabled: bool = False,
        echo_aec_order: int = 256,  # noqa: ARG002 -- kept for config compat
    ) -> None:
        if not _HAS_SD:
            raise ImportError("sounddevice is required for spoke audio")
        self.mic_sample_rate = mic_sample_rate
        self.mic_channels = mic_channels
        self.mic_chunk_frames = mic_chunk_frames
        self.mic_device = mic_device
        self.mic_volume = mic_volume
        self.spk_sample_rate = spk_sample_rate
        self.spk_device = spk_device
        self._aec: SpeexAEC | None = (
            SpeexAEC(sample_rate=self.MIC_SAMPLE_RATE, frame_size=mic_chunk_frames)
            if echo_aec_enabled and _HAS_SPEEX
            else None
        )
        self._mic_stream = None
        self._spk_stream = None
        self._playback_ref = np.empty(0, dtype=np.float32)
        self._callback = None

    def start_mic(self, callback) -> None:
        self._callback = callback
        logger.info("Opening mic: device=%s, rate=%d, channels=%d, blocksize=%d",
                    self.mic_device, self.mic_sample_rate, self.mic_channels, self.mic_chunk_frames)
        self._mic_stream = sd.InputStream(
            samplerate=self.mic_sample_rate,
            channels=self.mic_channels,
            blocksize=self.mic_chunk_frames,
            device=self.mic_device,
            callback=self._mic_cb,
            latency="low",
        )
        self._mic_stream.start()
        logger.info("Mic started")

    def stop_mic(self) -> None:
        if self._mic_stream:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

    def _mic_cb(self, indata, _frames, _time, status) -> None:
        if status:
            logger.warning("Mic status: %s", status)
        logger.debug("Mic callback: shape=%s, status=%s", indata.shape, status)
        samples = np.ascontiguousarray(indata[:, 0], dtype=np.float32) * self.mic_volume

        # Normalize audio to [-1, 1] range for consistent ASR performance
        peak = np.max(np.abs(samples))
        if peak > 1.0:
            samples = samples / peak
        elif peak < 0.01 and len(samples) > 0:  # Skip very quiet audio
            return

        if self._aec is not None and len(self._playback_ref) >= self._aec.frame_size:
            samples = self._aec.process(samples, self._playback_ref)
        if self._callback:
            self._callback(samples)

    def start_speaker(self) -> None:
        try:
            self._spk_stream = sd.OutputStream(
                samplerate=self.spk_sample_rate,
                channels=1,
                device=self.spk_device,
                latency="low",
            )
            self._spk_stream.start()
            logger.info("Speaker: %d Hz", self.spk_sample_rate)
        except Exception:
            logger.warning("Speaker output unavailable (TTS will not play)")
            self._spk_stream = None

    def stop_speaker(self) -> None:
        if self._spk_stream:
            try:
                self._spk_stream.stop()
                self._spk_stream.close()
            except Exception:
                pass
            self._spk_stream = None

    def play_audio(self, samples: NDArray[np.float32]) -> None:
        if len(samples) == 0:
            return
        if self._spk_stream is None:
            return  # No speaker available
        if self.spk_sample_rate != self.MIC_SAMPLE_RATE:
            from scipy import signal as scipy_signal
            divisor = math.gcd(self.MIC_SAMPLE_RATE, self.spk_sample_rate)
            samples = scipy_signal.resample_poly(
                samples, self.spk_sample_rate // divisor, self.MIC_SAMPLE_RATE // divisor,
            ).astype(np.float32)
        self._playback_ref = samples
        self._aec.update_reference(samples)
        try:
            sd.play(samples, self.spk_sample_rate, blocking=False)
        except Exception:
            logger.exception("Speaker playback error")

    def shutdown(self) -> None:
        self.stop_mic()
        self.stop_speaker()

