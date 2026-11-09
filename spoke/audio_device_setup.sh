#!/usr/bin/env bash
# GLaDOS Spoke Audio Device Configuration (ALSA)
# Automated: finds mic, tests speaker through mic, asks final confirmation.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/spoke_config.yaml"

echo "=== GLaDOS Spoke Audio Device Setup ==="
echo ""

# =============================================================================
# Step 1: Find the working microphone
# =============================================================================
echo "--- Step 1: Identifying Microphone ---"
echo ""

# USB headset is typically hw:4 on Raspberry Pi / Orange Pi
SELECTED_MIC="hw:4,0"
if arecord -D "$SELECTED_MIC" -f S16_LE -r 16000 -c 1 -d 3 /tmp/glados-mic-test.wav 2>/dev/null && [ -s /tmp/glados-mic-test.wav ]; then
    echo "USB headset mic (hw:4,0) recorded successfully."
else
    echo "hw:4,0 not found or failed. Scanning all mics..."
    SELECTED_MIC=""
    MIC_DEVICES=()
    while IFS= read -r line; do
        card=$(echo "$line" | grep -oP 'card \K[0-9]+')
        device=$(echo "$line" | grep -oP 'device \K[0-9]+' | head -1)
        [ -n "$card" ] && [ -n "$device" ] && MIC_DEVICES+=("hw:${card},${device}")
    done < <(arecord -l 2>/dev/null | grep 'device 0:')

    for mic in "${MIC_DEVICES[@]}"; do
        if arecord -D "$mic" -f S16_LE -r 16000 -c 1 -d 3 /tmp/glados-mic-test.wav 2>/dev/null && [ -s /tmp/glados-mic-test.wav ]; then
            SELECTED_MIC="$mic"
            echo "Found working mic: $mic"
            break
        fi
    done
    rm -f /tmp/glados-mic-test.wav

    if [ -z "$SELECTED_MIC" ]; then
        echo "ERROR: No microphone recorded any audio!"
        exit 1
    fi
fi

echo "Selected microphone: $SELECTED_MIC"
echo ""

# =============================================================================
# Step 2: Find the working speaker on the same card
# =============================================================================
echo "--- Step 2: Identifying Speaker ---"
echo ""

MIC_CARD=$(echo "$SELECTED_MIC" | grep -oP '\d+' | head -1)
SELECTED_SPEAKER="hw:${MIC_CARD},0"

# Check if this card has speakers
if aplay -l 2>/dev/null | grep -q "card $MIC_CARD"; then
    echo "Testing speaker on same card as mic: $SELECTED_SPEAKER..."
    echo "(You should hear a test tone through the headset speaker)"
    sleep 1
    sox -n -r 22050 /tmp/glados-test-tone.wav synth 3 sine 1000 2>/dev/null
    aplay -D "$SELECTED_SPEAKER" /tmp/glados-test-tone.wav 2>/dev/null
    rm -f /tmp/glados-test-tone.wav
    echo "Test tone played."
else
    echo "No speakers on card $MIC_CARD. Scanning all speakers..."
    SPEAKER_DEVICES=()
    while IFS= read -r line; do
        card=$(echo "$line" | grep -oP 'card \K[0-9]+')
        device=$(echo "$line" | grep -oP 'device \K[0-9]+' | head -1)
        [ -n "$card" ] && [ -n "$device" ] && SPEAKER_DEVICES+=("hw:${card},${device}")
    done < <(aplay -l 2>/dev/null | grep 'device 0:')

    if [ ${#SPEAKER_DEVICES[@]} -gt 0 ]; then
        SELECTED_SPEAKER="${SPEAKER_DEVICES[0]}"
        echo "Testing: $SELECTED_SPEAKER"
        sox -n -r 22050 /tmp/glados-test-tone.wav synth 3 sine 1000 2>/dev/null
        aplay -D "$SELECTED_SPEAKER" /tmp/glados-test-tone.wav 2>/dev/null
        rm -f /tmp/glados-test-tone.wav
    fi
fi

echo "Selected speaker: $SELECTED_SPEAKER"
echo ""

# =============================================================================
# Step 3: Test speaker through mic (loopback test)
# =============================================================================
echo "--- Step 3: Loopback Test (Speaker → Mic) ---"
echo ""

echo "Playing test tone through speaker and recording with mic..."
# Generate a tone and record at the same time
sox -n -r 22050 /tmp/glados-test-tone.wav synth 3 sine 1000 2>/dev/null

# Play tone and record simultaneously
aplay -D "$SELECTED_SPEAKER" /tmp/glados-test-tone.wav 2>/dev/null &
SPK_PID=$!
arecord -D "$SELECTED_MIC" -f S16_LE -r 16000 -c 1 -d 5 /tmp/glados-loopback.wav 2>/dev/null &
REC_PID=$!
wait $SPK_PID 2>/dev/null || true
wait $REC_PID 2>/dev/null || true
rm -f /tmp/glados-test-tone.wav

if [ -s /tmp/glados-loopback.wav ]; then
    echo "Recording saved. Playing it back..."
    aplay /tmp/glados-loopback.wav 2>/dev/null
    echo "(If you hear the test tone, the audio path is working)"
else
    echo "Loopback recording failed!"
fi
rm -f /tmp/glados-loopback.wav
echo ""

# =============================================================================
# Step 4: Final confirmation and config generation
# =============================================================================
echo "--- Step 4: Final Confirmation ---"
echo ""
echo "Summary:"
echo "  Microphone: $SELECTED_MIC"
echo "  Speaker:    $SELECTED_SPEAKER"
echo ""

# Use a simple read with a default
read -r -p "Are these correct? [Y/n] " CONFIRM
CONFIRM=${CONFIRM:-Y}
if [[ ! "$CONFIRM" =~ ^[Nn]$ ]]; then
    echo "Confirmed. Generating config..."
else
    echo "Please edit spoke_config.yaml manually."
    exit 0
fi

# =============================================================================
# Step 5: Write spoke_config.yaml
# =============================================================================
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
echo "Run: source $SCRIPT_DIR/.venv/bin/activate && glados-spoke --config $CONFIG_FILE"
