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


def _alsa_hw_to_device(hw: str) -> int | None:
    """Convert ALSA hw:X,Y string to sounddevice device index."""
    try:
        parts = hw.replace("hw:", "").split(",")
        if len(parts) != 2:
            return None
        
        card = int(parts[0])
        device = int(parts[1])
        
        for i, dev in enumerate(sd.query_devices()):
            name = dev.get("name", "")
            if f"hw:{card},{device}" in name or f"[hw:{card},{device}]" in name:
                logger.info("sounddevice device %d matches ALSA %s: %s", i, hw, name)
                return i
    except Exception as e:
        logger.warning("Failed to convert ALSA %s to device index: %s", hw, e)
    
    return None


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


# --- Lightweight MFCC-based audio similarity (no extra deps) ---

_MFCC_N_MFCC = 13
_MFCC_N_FFT = 512
_MFCC_HOP = 256
_MFCC_N_MELS = 40


def _mel_weights(n_mels: int = _MFCC_N_MELS, n_fft: int = _MFCC_N_FFT,
                 sr: int = 16000) -> NDArray[np.float64]:
    """Compute triangular mel filterbank weights."""
    # Convert mel to Hz
    mel_low = 0.0
    mel_high = 2595.0 * math.log10(1.0 + (sr / 2.0) / 700.0)
    mel_points = np.linspace(mel_low, mel_high, n_mels + 2)
    freq_points = 700.0 * (10.0 ** (mel_points / 2595.0) - 1.0)

    bin_indices = np.floor((n_fft + 1) * freq_points / sr)
    weights = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)

    for m in range(n_mels):
        f_m = int(bin_indices[m])
        f_m1 = int(bin_indices[m + 1])
        f_m2 = int(bin_indices[m + 2])

        if f_m2 > f_m1:
            weights[m, f_m1:f_m2] = np.linspace(0, 1, f_m2 - f_m1)
        if f_m1 > f_m:
            weights[m, f_m:f_m1] = np.linspace(1, 0, f_m1 - f_m)

    return weights


def _mfcc(audio: NDArray[np.float32], n_mfcc: int = _MFCC_N_MFCC) -> NDArray[np.float64]:
    """Extract simplified MFCC features from an audio sample."""
    n_fft = _MFCC_N_FFT
    # Short-time Fourier transform
    window = np.hanning(len(audio) + 1)[:len(audio)]
    stft = np.abs(np.fft.rfft(audio * window, n_fft)) ** 2

    # Apply mel filterbank
    fb = _mel_weights()
    energy = fb @ stft

    # Log energy with floor
    log_energy = np.log10(np.maximum(energy, 1e-10))

    # Discrete cosine transform → MFCCs
    dct = np.fft.dct(log_energy, type=2, axis=0)
    return dct[:n_mfcc]


def _audio_similarity(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """Cosine similarity between MFCC fingerprints of two audio clips."""
    mfcc_a = _mfcc(a)
    mfcc_b = _mfcc(b)

    norm_a = math.sqrt(float(np.dot(mfcc_a, mfcc_a)))
    norm_b = math.sqrt(float(np.dot(mfcc_b, mfcc_b)))

    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0

    return float(np.dot(mfcc_a, mfcc_b)) / (norm_a * norm_b)


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
        
        # Convert ALSA hw:X,Y to sounddevice index if needed
        if isinstance(mic_device, str):
            converted = _alsa_hw_to_device(mic_device)
            if converted is not None:
                self.mic_device = converted
                logger.info("Converted ALSA mic %s to sounddevice index %d", mic_device, converted)
            else:
                logger.warning("Could not convert ALSA mic %s to index, using as-is", mic_device)
                self.mic_device = mic_device
        else:
            self.mic_device = mic_device
        
        self.mic_volume = mic_volume
        self.spk_sample_rate = spk_sample_rate
        
        # Convert ALSA hw:X,Y to sounddevice index if needed
        if isinstance(spk_device, str):
            converted = _alsa_hw_to_device(spk_device)
            if converted is not None:
                self.spk_device = converted
                logger.info("Converted ALSA spk %s to sounddevice index %d", spk_device, converted)
            else:
                logger.warning("Could not convert ALSA spk %s to index, using as-is", spk_device)
                self.spk_device = spk_device
        else:
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

        # --- Echo detection: compare mic audio with recent speaker output ---
        if (self._aec is not None
                and len(self._playback_ref) >= self._aec.frame_size
                and self.spk_sample_rate >= 16000):
            # Resample reference to mic rate
            if self.spk_sample_rate != self.mic_sample_rate:
                from scipy import signal as scipy_signal
                divisor = math.gcd(self.mic_sample_rate, self.spk_sample_rate)
                ref = scipy_signal.resample_poly(
                    self._playback_ref,
                    self.mic_sample_rate // divisor,
                    self.spk_sample_rate // divisor,
                ).astype(np.float32)
            else:
                ref = self._playback_ref

            # Check if mic audio matches the speaker reference (echo)
            if len(ref) >= self._aec.frame_size:
                ref_slice = ref[:self._aec.frame_size]
                if _audio_similarity(samples, ref_slice) > 0.70:
                    logger.debug("Spoke echo detected — suppressing mic frame")
                    # Still pass the frame but zero it out to prevent hub from
                    # hearing GLaDOS's own voice (catches cases where AEC missed)
                    samples = np.zeros_like(samples)

            # Run AEC on the (possibly suppressed) audio
            if self._aec is not None and len(ref) >= self._aec.frame_size:
                samples = self._aec.process(samples, ref)
        elif self._aec is not None and len(self._playback_ref) >= self._aec.frame_size:
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
        # Store original (unresampled) samples as the AEC reference.
        # sd.play() will handle resampling internally; the AEC resamples
        # the reference to the mic rate in the callback.
        self._playback_ref = samples.astype(np.float32)
        try:
            sd.play(samples, self.spk_sample_rate, blocking=False)
        except Exception:
            logger.exception("Speaker playback error")

    def shutdown(self) -> None:
        self.stop_mic()
        self.stop_speaker()

