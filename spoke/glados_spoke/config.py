"""Configuration for the GLaDOS spoke client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class SpokeConfig(BaseModel):
    """Configuration for a GLaDOS spoke endpoint."""

    # --- Hub connection ---
    hub_host: str = Field(default="127.0.0.1", description="Hub hostname or IP.")
    hub_port: int = Field(default=5051, ge=1, le=65535, description="Hub WebSocket port.")

    # --- Reconnection ---
    reconnect_base_delay: float = Field(default=1.0, ge=0.1, le=30.0)
    reconnect_max_delay: float = Field(default=30.0, ge=1.0)
    reconnect_jitter: float = Field(default=0.5, ge=0.0, le=1.0)
    reconnect_attempts: int | None = Field(default=None)
    heartbeat_interval_s: float = Field(default=5.0, ge=1.0, le=60.0)

    # --- Audio capture (mic to hub) ---
    mic_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    mic_channels: int = Field(default=1, ge=1, le=2)
    mic_chunk_frames: int = Field(default=512, ge=128, le=8192)
    mic_device: int | str | None = Field(default=0, description="XVF3800 mic sounddevice index (always 0)")
    mic_volume: float = Field(default=1.0, ge=0.0, le=5.0)

    # --- Audio playback (hub to spoke) ---
    spk_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    spk_device: int | str | None = Field(default=0, description="XVF3800 speaker sounddevice index (always 0)")
    spk_volume: float = Field(default=1.0, ge=0.0, le=1.0, description="Speaker master volume, 0.0-1.0 (1.0 = 100%)")

    # --- Jitter buffer ---
    jitter_buffer_min_ms: int = Field(default=80, ge=10, le=500)
    jitter_buffer_max_ms: int = Field(default=400, ge=50, le=2000)
    jitter_adapt_speed: float = Field(default=0.1, ge=0.01, le=1.0)

    # --- Echo detection ---
    echo_aec_enabled: bool = Field(default=False)
    echo_aec_order: int = Field(default=256, ge=16, le=4096)
    echo_mute_on_detection_ms: int = Field(default=0, ge=0, le=2000)
    echo_text_similarity_threshold: float = Field(default=0.7)
    echo_text_min_length: int = Field(default=4)
    echo_text_recent_turns: int = Field(default=3)
    echo_waveform_threshold: float = Field(default=0.6)

    # --- Native audio mode ---
    native_audio: bool = Field(default=True)
    native_audio_max_duration_s: float = Field(default=30.0)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    @classmethod
    def from_file(cls, path: str | Path) -> "SpokeConfig":
        """Load SpokeConfig from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        data = yaml.safe_load(path.read_text()) or {}
        # Config may be nested under a "spoke" key
        spoke_data = data.get("spoke", data)
        return cls(**spoke_data)
