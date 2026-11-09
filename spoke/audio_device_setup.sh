#!/usr/bin/env bash
# GLaDOS Spoke Audio Device Configuration (ALSA)
# Tests speakers and microphone, then updates spoke_config.yaml.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/spoke_config.yaml"

echo "=== GLaDOS Spoke Audio Device Setup ==="
echo ""

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

# =============================================================================
# Step 1: Enumerate and test speaker outputs
# =============================================================================
echo "--- Step 1: Speaker Output Selection ---"
echo ""

SPEAKER_DEVICES=()
while IFS= read -r line; do
    card=$(echo "$line" | grep -oP 'card \K[0-9]+')
    device=$(echo "$line" | grep -oP 'device \K[0-9]+' | head -1)
    name=$(echo "$line" | grep -oP '\] \K.*' | sed 's/,$//')
    if [ -n "$card" ] && [ -n "$device" ]; then
        SPEAKER_DEVICES+=("hw:${card},${device}")
    fi
done < <(aplay -l 2>/dev/null | grep 'device 0:')

if [ ${#SPEAKER_DEVICES[@]} -eq 0 ]; then
    echo "No speaker outputs found."
    echo "Run 'aplay -l' to see available devices."
    SELECTED_SPEAKER="4"
else
    for i in "${!SPEAKER_DEVICES[@]}"; do
        echo "  [$((i+1))] ${SPEAKER_DEVICES[$i]}"
    done
    echo ""
    echo "Playing a test tone through each speaker (3 seconds each)..."
    echo "Press Ctrl+C to skip this test."
    echo ""

    for i in "${!SPEAKER_DEVICES[@]}"; do
        dev="${SPEAKER_DEVICES[$i]}"
        echo -n "  [$((i+1))] $dev ... "
        speaker-test -t sine -f 1000 -l 1 -c 2 -D "$dev" 2>/dev/null &
        SPK_PID=$!
        sleep 3
        kill $SPK_PID 2>/dev/null || true
        wait $SPK_PID 2>/dev/null || true
        echo "done."
    done
    echo ""

    if ask_yes_no "Did you hear audio?"; then
        echo "Which speaker works? (enter the number, default: 1)"
        read -rp "> " -i "1" selected_num
        selected_num=${selected_num:-1}
        if [ "$selected_num" -ge 1 ] && [ "$selected_num" -le "${#SPEAKER_DEVICES[@]}" ]; then
            SELECTED_SPEAKER="${SPEAKER_DEVICES[$((selected_num-1))]}"
        else
            SELECTED_SPEAKER="${SPEAKER_DEVICES[0]}"
        fi
    else
        echo "No speaker detected. Defaulting to first device: ${SPEAKER_DEVICES[0]}"
        SELECTED_SPEAKER="${SPEAKER_DEVICES[0]}"
    fi
fi

echo "Selected speaker: $SELECTED_SPEAKER"
echo ""

# =============================================================================
# Step 2: Enumerate and test microphone inputs
# =============================================================================
echo "--- Step 2: Microphone Selection ---"
echo ""

MIC_DEVICES=()
while IFS= read -r line; do
    card=$(echo "$line" | grep -oP 'card \K[0-9]+')
    device=$(echo "$line" | grep -oP 'device \K[0-9]+' | head -1)
    name=$(echo "$line" | grep -oP '\] \K.*' | sed 's/,$//')
    if [ -n "$card" ] && [ -n "$device" ]; then
        MIC_DEVICES+=("hw:${card},${device}")
    fi
done < <(arecord -l 2>/dev/null | grep 'device 0:')

if [ ${#MIC_DEVICES[@]} -eq 0 ]; then
    echo "No microphone inputs found."
    echo "Run 'arecord -l' to see available devices."
    SELECTED_MIC="hw:4,0"
else
    for i in "${!MIC_DEVICES[@]}"; do
        echo "  [$((i+1))] ${MIC_DEVICES[$i]}"
    done
    echo ""
    echo "Recording from each mic (3 seconds), then playing it back..."
    echo "Press Ctrl+C to skip."
    echo ""

    for i in "${!MIC_DEVICES[@]}"; do
        mic="${MIC_DEVICES[$i]}"
        echo -n "  [${i+1}] Recording from: $mic (3 seconds)... "
        arecord -D "$mic" -f S16_LE -r 16000 -c 1 -d 3 /tmp/glados-mic-test.wav 2>/dev/null &
        REC_PID=$!
        sleep 3
        kill $REC_PID 2>/dev/null || true
        wait $REC_PID 2>/dev/null || true

        if [ -f /tmp/glados-mic-test.wav ]; then
            echo "done. (listening back...)"
            aplay /tmp/glados-mic-test.wav 2>/dev/null
            if ask_yes_no "Did you hear your voice?"; then
                SELECTED_MIC="$mic"
                echo "Selected mic: $SELECTED_MIC"
                rm -f /tmp/glados-mic-test.wav
                break
            fi
        else
            echo "failed to record."
        fi
        sleep 1
    done
    rm -f /tmp/glados-mic-test.wav

    if [ -z "${SELECTED_MIC:-}" ]; then
        SELECTED_MIC="${MIC_DEVICES[0]}"
        echo "No mic confirmed. Defaulting to: $SELECTED_MIC"
    fi
fi
echo ""

# =============================================================================
# Step 3: Update spoke_config.yaml
# =============================================================================
echo "--- Step 3: Updating Configuration ---"
echo ""

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
  spk_device: $(echo "$SELECTED_SPEAKER" | grep -oP 'hw:\K[0-9]+')

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
