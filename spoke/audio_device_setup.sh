#!/usr/bin/env bash
# GLaDOS Spoke Audio Device Auto-Configuration
# Tests speakers and microphone, then configures PipeWire WebRTC AEC.
set -e

echo "=== GLaDOS Audio Device Auto-Configuration ==="
echo "This section will test your microphone and speaker(s) to set up"
echo "PipeWire echo cancellation. You'll hear test sounds and record"
echo "a few seconds of your voice."
echo ""

# =============================================================================
# Install audio testing tools
# =============================================================================
echo "Installing audio testing utilities..."
apt-get install -y --no-install-recommends pipewire-bin alsa-utils 2>/dev/null || true

# =============================================================================
# Check if PipeWire is running
# =============================================================================
echo ""
echo "Checking PipeWire..."
if ! systemctl --user is-active pipewire-pulse &>/dev/null; then
    echo "PipeWire is not running. Starting it now..."
    systemctl --user daemon-reload
    systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || {
        echo "Warning: Could not start PipeWire user services."
        echo "Please start them manually: systemctl --user enable --now pipewire pipewire-pulse wireplumber"
        echo ""
        exit 1
    }
    sleep 2
fi

if ! systemctl --user is-active pipewire-pulse &>/dev/null; then
    echo "PipeWire still not running after startup attempt."
    echo "Please run: systemctl --user start pipewire-pulse"
    echo ""
    exit 1
fi

echo "PipeWire is running."

# =============================================================================
# Enumerate and test speaker outputs
# =============================================================================
echo ""
echo "--- Step 1: Speaker Output Selection ---"
echo "The following speaker outputs were found:"
echo ""

SPEAKER_OUTPUTS=()
while IFS=$'\t' read -r dev_name dev_desc; do
    SPEAKER_OUTPUTS+=("$dev_name")
done < <(pw-cli list-objects 2>/dev/null | awk '
    /node\.name/ { name = $3 }
    /media\.name/ { desc = $3 }
    /Audio\/Sink/ { print name "\t" desc }
')

if [ ${#SPEAKER_OUTPUTS[@]} -eq 0 ]; then
    echo "No speaker outputs found. Try: pactl list short sinks"
    SELECTED_SPEAKER="null"
else
    for i in "${!SPEAKER_OUTPUTS[@]}"; do
        echo "  [$((i+1))] ${SPEAKER_OUTPUTS[$i]}"
    done
    echo ""
    echo "Playing a test tone through each speaker (3 seconds each)..."
    echo "Press Ctrl+C to skip this loop."
    echo ""

    for i in "${!SPEAKER_OUTPUTS[@]}"; do
        dev="${SPEAKER_OUTPUTS[$i]}"
        echo -n "  [$((i+1))] $dev ... "
        speaker-test -t sine -f 1000 -l 1 -c 2 -s 0 -D "$dev" 2>/dev/null &
        SPK_PID=$!
        sleep 3
        kill $SPK_PID 2>/dev/null || true
        wait $SPK_PID 2>/dev/null || true
        echo "done."
    done
    echo ""

    echo "Which speaker output works? (enter the number, default: 1)"
    read -rp "> " -i "1" selected_num
    selected_num=${selected_num:-1}
    if [ "$selected_num" -ge 1 ] && [ "$selected_num" -le "${#SPEAKER_OUTPUTS[@]}" ]; then
        SELECTED_SPEAKER="${SPEAKER_OUTPUTS[$((selected_num-1))]}"
        echo "Selected speaker: $SELECTED_SPEAKER"
    else
        echo "Invalid selection. Using first device: ${SPEAKER_OUTPUTS[0]}"
        SELECTED_SPEAKER="${SPEAKER_OUTPUTS[0]}"
    fi
fi

# =============================================================================
# Enumerate and test microphone inputs
# =============================================================================
echo ""
echo "--- Step 2: Microphone Selection ---"
echo "The following microphone inputs were found:"
echo ""

MIC_INPUTS=()
while IFS=$'\t' read -r dev_name dev_desc; do
    MIC_INPUTS+=("$dev_name")
    echo "  [${#MIC_INPUTS[@]}] $dev_name ($dev_desc)"
done < <(pw-cli list-objects 2>/dev/null | awk '
    /node\.name/ { name = $3 }
    /media\.name/ { desc = $3 }
    /Audio\/Source/ { print name "\t" desc }
')

if [ ${#MIC_INPUTS[@]} -eq 0 ]; then
    echo "No microphone inputs found. Try: pactl list short sources"
    SELECTED_MIC="null"
else
    echo ""
    echo "Microphone test: you will record 3 seconds of audio for each mic."
    echo "After each recording, you will hear it back to verify it works."
    echo "Press Ctrl+C to skip."
    echo ""

    for i in "${!MIC_INPUTS[@]}"; do
        mic="${MIC_INPUTS[$i]}"
        echo -n "  [${i+1}] Recording from: $mic (3 seconds)... "
        arecord -D "$mic" -f S16_LE -r 16000 -c 1 -d 3 /tmp/glados-mic-test.wav 2>/dev/null &
        REC_PID=$!
        sleep 3
        kill $REC_PID 2>/dev/null || true
        wait $REC_PID 2>/dev/null || true

        if [ -f /tmp/glados-mic-test.wav ]; then
            echo "done. (listening back...)"
            aplay /tmp/glados-mic-test.wav 2>/dev/null
        else
            echo "failed to record."
        fi
        sleep 1
    done
    rm -f /tmp/glados-mic-test.wav

    echo ""
    echo "Which microphone works? (enter the number, default: 1)"
    read -rp "> " -i "1" selected_num
    selected_num=${selected_num:-1}
    if [ "$selected_num" -ge 1 ] && [ "$selected_num" -le "${#MIC_INPUTS[@]}" ]; then
        SELECTED_MIC="${MIC_INPUTS[$((selected_num-1))]}"
        echo "Selected mic: $SELECTED_MIC"
    else
        echo "Invalid selection. Using first device: ${MIC_INPUTS[0]}"
        SELECTED_MIC="${MIC_INPUTS[0]}"
    fi
fi

# =============================================================================
# Configure PipeWire WebRTC AEC
# =============================================================================
echo ""
echo "--- Step 3: Configuring PipeWire Echo Cancellation ---"
echo ""
echo "Writing PipeWire AEC config to /etc/pipewire/pipewire.conf.d/99-aec.conf..."
sudo mkdir -p /etc/pipewire/pipewire.conf.d
sudo tee /etc/pipewire/pipewire.conf.d/99-aec.conf > /dev/null << PWEOF
# GLaDOS Spoke - PipeWire WebRTC Echo Cancellation
# Auto-generated on $(date -Iseconds)
# Speaker output: $SELECTED_SPEAKER
# Microphone input: $SELECTED_MIC

context.objects = [
    {
        factory = "adapter"
        args = {
            node.name          = "Echo Cancel Source"
            media.class        = Audio/Source
            audio.position     [ MONO ]
            filter.hear-me     = true
            filter.listen-on   = "$SELECTED_SPEAKER"
            filter.hear-me.aec.method = "webrtc"
            filter.hear-me.aec.args.sample-rate = 16000
            filter.hear-me.aec.args.frame-size = 512
            filter.hear-me.aec.args.echo-tail = 512
        }
    }
]
PWEOF

echo "Restarting PipeWire to apply AEC config..."
systemctl --user restart pipewire pipewire-pulse wireplumber
sleep 2

# Verify AEC source is active
if pactl list sources short 2>/dev/null | grep -q "Echo Cancel Source"; then
    echo ""
    echo "SUCCESS! Echo cancellation is active."
    pactl list sources short | grep -i "echo cancel"
else
    echo ""
    echo "Warning: Echo cancellation source not detected after restart."
    echo "Check PipeWire logs: journalctl --user -u pipewire -n 50"
    echo "You can manually edit the config at:"
    echo "  /etc/pipewire/pipewire.conf.d/99-aec.conf"
fi

echo ""
echo "=== Audio Setup Complete ==="
echo "  Speaker output: $SELECTED_SPEAKER"
echo "  Microphone:     $SELECTED_MIC"
echo "  AEC method:     WebRTC (system-level)"
echo ""
echo "Start the spoke with:"
echo "  source /opt/GLaDOS/spoke/.venv/bin/activate"
echo "  glados-spoke --config /opt/GLaDOS/configs/spoke_config.yaml"
echo ""
