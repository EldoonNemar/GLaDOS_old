#!/usr/bin/env bash
# Install GLaDOS Spoke client
# Usage: ./install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "=== GLaDOS Spoke Installer ==="

# Install system dependencies
echo "Installing system dependencies..."
apt-get update && apt-get install -y libportaudio2 libspeexdsp-dev libstdc++6 python3-venv

# Check if venv already exists
if [ -d "$VENV_DIR" ]; then
    echo "Virtual environment already exists at $VENV_DIR"
    read -p "Recreate it? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Activating existing venv..."
        source "$VENV_DIR/bin/activate"
        pip install -e .
        echo "Installation complete. Run: source $VENV_DIR/bin/activate && glados-spoke"
        exit 0
    fi
    rm -rf "$VENV_DIR"
fi

echo "Creating virtual environment..."
python3 -m venv "$VENV_DIR"

echo "Activating venv and installing..."
source "$VENV_DIR/bin/activate"
pip install -e .

echo ""
echo "=== Installation complete ==="
echo "To use:"
echo "  source $VENV_DIR/bin/activate"
echo "  glados-spoke --hub <hub-ip> --port <port>"
echo ""
echo "Or with config file:"
echo "  glados-spoke --config spoke_config.yaml"