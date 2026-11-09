#!/usr/bin/env bash
# GLaDOS Spoke Audio Device Configuration (ALSA)
# Tests mic first to identify the USB headset, then tests speaker on that card.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/spoke_config.yaml"

# =============================================================================
# Helper: ask yes/no
# =============================================================================
ask_yes_no() {
    local question="$1"
    local answer="n"
    while [[ ! "$answer" =~ ^[YyNn]$ ]]; do
        read -rp "$question [Y/n] " -i "Y" answer
        answer=${answer:-Y}
    done
    [[ "$answer" =~ ^[Yy]$ ]]
}

echo "=== GLaDOS Spoke Audio Device Setup ==="
echo ""

# =============================================================================
# Step 1: Find the USB headset mic (hw:4,0 typically)
# =============================================================================
echo "--- Step 1: Identify Microphone ---"
echo ""

# Check if hw:4 (USB headset) exists — it's the most common on Raspberry Pi
USB_MIC_FOUND=false
for i in "4" "3" "0"; do
    if arecord -l 2>/dev/null | grep -q "card $i"; then
        USB_MIC_FOUND=true
        break
    fi
done

if [ "$USB_MIC_FOUND" = true ]; then
    SELECTED_MIC="hw:4,0"
    echo "Detected USB headset mic (hw:4,0). Testing..."
    echo "Speak into the mic for 3 seconds..."
    sleep 1
    arecord -D "hw:4,0" -f S16_LE -r 16000 -c 1 -d 3 /tmp/glados-mic-test.wav 2>/dev/null
    echo "Playing back your recording..."
    aplay /tmp/glados-mic-test.wav 2>/dev/null
    rm -f /tmp/glados-mic-test.wav
    if ask_yes_no "Did you hear yourself clearly?"; then
        echo "Confirmed: microphone is $SELECTED_MIC"
    else
        echo "USB headset mic didn't work. Scanning for other mics..."
        SELECTED_MIC=""
    fi
fi

if [ -z "${SELECTED_MIC:-}" ]; then
    echo ""
    echo "No USB headset detected. Scanning all available mics..."
    MIC_DEVICES=()
    while IFS= read -r line; do
        card=$(echo "$line" | grep -oP 'card \K[0-9]+')
        device=$(echo "$line" | grep -oP 'device \K[0-9]+' | head -1)
        if [ -n "$card" ] && [ -n "$device" ]; then
            MIC_DEVICES+=("hw:${card},${device}")
        fi
    done < <(arecord -l 2>/dev/null | grep 'device 0:')

    if [ ${#MIC_DEVICES[@]} -eq 0 ]; then
        echo "No microphones found at all!"
        SELECTED_MIC="hw:0,0"
    else
        for i in "${!MIC_DEVICES[@]}"; do
            mic="${MIC_DEVICES[$i]}"
            echo -n "  [${i+1}] Recording from $mic (3 seconds)... "
            arecord -D "$mic" -f S16_LE -r 16000 -c 1 -d 3 /tmp/glados-mic-test.wav 2>/dev/null &
            sleep 3
            kill $! 2>/dev/null || true
            wait $! 2>/dev/null || true

            if [ -f /tmp/glados-mic-test.wav ]; then
                echo "listening back..."
                aplay /tmp/glados-mic-test.wav 2>/dev/null
                if ask_yes_no "Did you hear your voice?"; then
                    SELECTED_MIC="$mic"
                    echo "Confirmed: microphone is $SELECTED_MIC"
                    break
                fi
            else
                echo "failed to record."
            fi
            rm -f /tmp/glados-mic-test.wav
        done
    fi
fi

echo "Selected microphone: $SELECTED_MIC"
echo ""

# =============================================================================
# Step 2: Find the speaker on the SAME card as the mic
# =============================================================================
echo "--- Step 2: Identify Speaker ---"
echo ""

# Extract the card number from the mic device
MIC_CARD=$(echo "$SELECTED_MIC" | grep -oP '\d+(?=,)')
SPEAKER_CARD=$(echo "$SELECTED_MIC" | grep -oP '\d+' | head -1)

echo "Looking for speakers on card $SPEAKER_CARD (same as mic)..."

SELECTED_SPEAKER="hw:${SPEAKER_CARD},0"

# Test if this speaker works
TEST_TONE_FILE="/tmp/glados-test-tone.wav"
echo "Generating and playing test tone on $SELECTED_SPEAKER..."
sox -n -r 22050 "$TEST_TONE_FILE" synth 3 sine 1000 2>/dev/null || true
if [ -f "$TEST_TONE_FILE" ]; then
    aplay -D "$SELECTED_SPEAKER" "$TEST_TONE_FILE" 2>/dev/null
    rm -f "$TEST_TONE_FILE"
    if ask_yes_no "Did you hear the test tone?"; then
        echo "Confirmed: speaker is $SELECTED_SPEAKER"
    else
        echo "Test tone not heard. Scanning all speakers..."
        SELECTED_SPEAKER=""
    fi
fi

if [ -z "${SELECTED_SPEAKER:-}" ]; then
    echo "Scanning all available speakers..."
    SPEAKER_DEVICES=()
    while IFS= read -r line; do
        card=$(echo "$line" | grep -oP 'card \K[0-9]+')
        device=$(echo "$line" | grep -oP 'device \K[0-9]+' | head -1)
        if [ -n "$card" ] && [ -n "$device" ]; then
            SPEAKER_DEVICES+=("hw:${card},${device}")
        fi
    done < <(aplay -l 2>/dev/null | grep 'device 0:')

    for i in "${!SPEAKER_DEVICES[@]}"; do
        dev="${SPEAKER_DEVICES[$i]}"
        echo -n "  [${i+1}] Playing test tone on $dev (3 seconds)... "
        sox -n -r 22050 "$TEST_TONE_FILE" synth 3 sine 1000 2>/dev/null || true
        if [ -f "$TEST_TONE_FILE" ]; then
            aplay -D "$dev" "$TEST_TONE_FILE" 2>/dev/null
            rm -f "$TEST_TONE_FILE"
        fi
        if ask_yes_no "Did you hear the tone?"; then
            SELECTED_SPEAKER="$dev"
            echo "Confirmed: speaker is $SELECTED_SPEAKER"
            break
        fi
    done
fi

if [ -z "${SELECTED_SPEAKER:-}" ]; then
    echo "No speaker confirmed. Defaulting to: $SELECTED_SPEAKER"
fi

echo "Selected speaker: $SELECTED_SPEAKER"
echo ""

# =============================================================================
# Step 3: Update spoke_config.yaml
# =============================================================================
echo "--- Step 3: Updating Configuration ---"
echo ""

SPEAKER_CARD_NUM=$(echo "$SELECTED_SPEAKER" | grep -oP '\d+' | head -1)

cat > "$CONFIG_FILE" << YAMLEOF
# GLaDOS Spoke Client Configuration
# Auto-generated on $(date -Iseconds)

spoke:
  # --- Hub connection ---
  hub_host: "192.168.1.58"
  hub_port: 5051

  # --- Audio capture (mic to hub) ---
  mic_sample_rate: 16000
  mic_channels: 1
  mic_chunk_frames: 512
  mic_device: "$SELECTED_MIC"
  mic_volume: 1.0

  # --- Audio playback (hub to spoke) ---
  spk_sample_rate: 22050
  spk_device: $SPEAKER_CARD_NUM

  # --- Echo detection ---
  echo_text_similarity_threshold: 0.7
  echo_text_min_length: 4
  echo_text_recent_turns: 3
  echo_waveform_threshold: 0.6
  echo_aec_enabled: true
  echo_aec_order: 256
  echo_mute_on_detection_ms: 0

  # --- Native audio mode ---
  native_audio: true
  native_audio_max_duration_s: 30.0

  # --- Logging ---
  log_level: "INFO"
  heartbeat_interval_s: 5.0
YAMLEOF

echo "Configuration written to: $CONFIG_FILE"
echo ""
echo "=== Audio Setup Complete ==="
echo "  Speaker output: $SELECTED_SPEAKER"
echo "  Microphone:     $SELECTED_MIC"
echo ""
echo "Test it with:"
echo "  source $SCRIPT_DIR/.venv/bin/activate && glados-spoke --config $CONFIG_FILE"
