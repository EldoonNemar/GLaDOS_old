"""Whisper ASR transcriber wrapper for GLaDOS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray


class AudioTranscriber:
    """Whisper-based audio transcriber for GLaDOS."""

    def __init__(
        self,
        model_name: str = "base",
        language: str = "en",
        device: str = "cpu",
        **kwargs: Any,
    ) -> None:
        """Initialize Whisper transcriber.

        Args:
            model_name: Whisper model size (tiny, base, small, medium, large)
            language: Language code (e.g., 'en', 'es', 'fr')
            device: Device to run inference on ('cpu' or 'cuda')
        """
        import whisper

        self._whisper = whisper
        self._model = whisper.load_model(model_name, device=device)
        self._language = language
        self._device = device
        self._model_name = model_name
        print(f"Whisper ASR loaded: model={model_name}, device={device}, language={language}")

    def transcribe(self, audio: NDArray[Any]) -> str:
        """Transcribe audio array to text.

        Args:
            audio: Input audio as numpy float32 array (16kHz mono)

        Returns:
            Transcribed text string
        """
        result = self._model.transcribe(
            audio,
            language=self._language,
            fp16=False if self._device == "cpu" else None,
        )
        return result.get("text", "").strip()

    def transcribe_file(self, audio_path: Path) -> str:
        """Transcribe an audio file to text.

        Args:
            audio_path: Path to audio file

        Returns:
            Transcribed text string
        """
        result = self._model.transcribe(
            str(audio_path),
            language=self._language,
            fp16=False if self._device == "cpu" else None,
        )
        return result.get("text", "").strip()