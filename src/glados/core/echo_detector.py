"""Echo detector for hub-side ASR transcriptions.

When the hub receives an ASR transcription, it compares it against
recently spoken text to detect if the transcription is echo (GLaDOS
hearing her own output).  If echo is detected, the transcription is
silently discarded.

This runs on the hub, which knows exactly what text it sent to TTS.
"""

from __future__ import annotations

from collections import deque
import threading

from Levenshtein import distance as lev_distance

from ..observability import ObservabilityBus


class EchoDetector:
    """Detect echo in ASR transcriptions by comparing against recently spoken text."""

    def __init__(
        self,
        similarity_threshold: float = 0.7,
        min_length: int = 4,
        recent_turns: int = 3,
        bus: ObservabilityBus | None = None,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.min_length = min_length
        self.recent_turns = recent_turns
        self._bus = bus
        self._spoken: deque[str] = deque(maxlen=recent_turns)
        self._lock = threading.Lock()

    def record_spoken(self, text: str) -> None:
        """Record text that was spoken (called by AudioPlayer after TTS)."""
        with self._lock:
            if text:
                self._spoken.append(text)

    def check_echo(self, transcription: str) -> bool:
        """Check if a transcription is likely echo.

        Returns:
            True if the transcription matches recently spoken text (echo detected).
            False if it appears to be genuine user speech.
        """
        if len(transcription) < self.min_length:
            return False  # Too short to be meaningful

        with self._lock:
            spoken_texts = list(self._spoken)

        if not spoken_texts:
            return False

        trans_lower = transcription.lower().strip()

        for spoken in spoken_texts:
            spoken_lower = spoken.lower().strip()
            if len(spoken_lower) < self.min_length:
                continue

            # Levenshtein similarity: 1 - (distance / max_length)
            max_len = max(len(trans_lower), len(spoken_lower))
            if max_len == 0:
                continue
            similarity = 1.0 - (lev_distance(trans_lower, spoken_lower) / max_len)

            if similarity >= self.similarity_threshold:
                if self._bus:
                    self._bus.emit(
                        "echo", "detected", f"Echo detected (similarity={similarity:.2f})",
                        meta={"transcription": transcription[:100], "spoken": spoken[:100], "similarity": similarity},
                    )
                return True

        return False

    @property
    def recent_spoken(self) -> list[str]:
        """Return a copy of recently spoken text for diagnostics."""
        with self._lock:
            return list(self._spoken)

    def snapshot(self) -> dict:
        """Return diagnostics snapshot."""
        with self._lock:
            return {
                "threshold": self.similarity_threshold,
                "min_length": self.min_length,
                "recent_turns": self.recent_turns,
                "buffered_turns": len(self._spoken),
                "spoken_text": list(self._spoken),
            }

    def reset(self) -> None:
        """Clear the spoken text buffer."""
        with self._lock:
            self._spoken.clear()
