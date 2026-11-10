#!/usr/bin/env bash
# Install GLaDOS Spoke client
# Usage: ./install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "=== GLaDOS Spoke Installer ==="

# Install system dependencies
echo "Installing system dependencies..."
echo "NOTE: This requires sudo privileges. You may need to run this script with sudo."
if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running with sudo..."
    exec sudo "$0" "$@"
fi

apt-get update && apt-get install -y libspeex-dev libspeexdsp-dev libasound2-dev libportaudio2 libstdc++6 python3-venv swig g++

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
echo "  glados-spoke run --hub <hub-ip> --port <port>"
echo ""
echo "Or with config file:"
echo "  glados-spoke run --config spoke_config.yaml"
echo ""

# Run audio device setup
while true; do
    read -p "Configure audio devices? (y/N) " -n 1 -r REPLY
    echo
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        echo ""
        echo "Choose setup mode:"
        echo "  1) Auto-detect (recommended - picks USB headset if available)"
        echo "  2) Interactive (you pick each device)"
        echo "  3) List devices only"
        echo "  4) Skip for now"
        echo ""
        read -p "Select [1-4] (default: 1) " MODE
        MODE=${MODE:-1}

        case "$MODE" in
            1)
                echo ""
                echo "--- Auto-detecting audio devices ---"
                "$VENV_DIR/bin/python" "$SCRIPT_DIR/setup_audio.py" --auto --config "$SCRIPT_DIR/spoke_config.yaml"
                ;;
            2)
                echo ""
                echo "--- Manual device selection ---"
                "$VENV_DIR/bin/python" "$SCRIPT_DIR/setup_audio.py" --manual --config "$SCRIPT_DIR/spoke_config.yaml"
                ;;
            3)
                echo ""
                "$VENV_DIR/bin/python" "$SCRIPT_DIR/setup_audio.py" --list
                ;;
            4)
                echo "Skipping audio setup."
                ;;
            *)
                echo "Invalid option. Skipping audio setup."
                ;;
        esac
        break
    elif [[ "$REPLY" =~ ^[Nn]$ ]]; then
        echo "Skipping audio device setup."
        break
    fi
    echo "Please enter y or n."
done