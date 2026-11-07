# GLaDOS Hub — runs on your ROCm server alongside llama.cpp
#
# Usage:
#   docker build -f docker/hub.Dockerfile -t glados-hub .
#   docker compose up -d --build hub
#
# The hub connects to your llama_rocm container for inference.
# It runs TTS and audio relay on CPU (fast enough for relay-only workload).
# Whisper ASR uses ROCm GPU for fast transcription.

FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY configs/ ./configs/

# Install deps — editable install so resource_path resolves correctly
# Also installs onnxruntime-cpu for TTS/VAD and litestar for TTS API
RUN uv venv .venv && uv pip install -e ".[cpu,api]"

# Download required models (VAD + TTS)
RUN uv run --no-sync glados download

# --- Runtime ---
FROM python:3.12-slim

# Install uv for running commands
RUN pip install uv

# Runtime deps for audio I/O
RUN apt-get update && apt-get install -y \
    libportaudio2 \
    libstdc++6 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install ROCm for GPU acceleration (PyTorch with ROCm)
RUN pip install torch --index-url https://download.pytorch.org/whl/rocm6.2.4

WORKDIR /app

# Copy installed venv from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/configs /app/configs
COPY --from=builder /app/pyproject.toml /app/README.md /app/

EXPOSE 5050 5051

ENV PATH="/app/.venv/bin:$PATH"
CMD ["glados", "start", "--config", "configs/glados_websocket_config.yaml", "--tts-enabled"]
