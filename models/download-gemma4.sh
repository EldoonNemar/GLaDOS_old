#!/usr/bin/env bash
# Download the Gemma 4 E4B GGUF model for glados_llama.
# Requires: pip install huggingface_hub
#
# Usage:
#   ./models/download-gemma4.sh              # Downloads to ./models/
#   HF_TOKEN=hf_xxx ./models/download-gemma4.sh  # If model is gated
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL_DIR=models
MODEL_FILE="$MODEL_DIR/gemma-4-E4B-Q4_K_M.gguf"

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_FILE" ]; then
    echo "Model already exists: $MODEL_FILE"
    exit 0
fi

echo "Downloading gemma-4-E4B-Q4_K_M.gguf from Hugging Face..."
echo "This may take a while (~2 GB)..."
huggingface-cli download bartowski/gemma-4-E4B-Instruct-GGUF \
    gemma-4-E4B-Instruct-Q4_K_M.gguf \
    --local-dir "$MODEL_DIR" \
    --local-dir-use-symlinks false

mv "$MODEL_DIR/gemma-4-E4B-Instruct-Q4_K_M.gguf" "$MODEL_FILE"
echo "Done: $MODEL_FILE"
