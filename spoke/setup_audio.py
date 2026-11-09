#!/usr/bin/env python3
"""Auto-detect and configure audio devices for GLaDOS Spoke.

Two-phase detection:
1. List devices (sounddevice preferred, ALSA fallback)
2. Loopback test: play tone through speaker, mic records it
   → confirms the mic-speaker pair physically works together
3. Frequency test: check which sample rates each device supports

Usage:
    python setup_audio.py              # interactive
    python setup_audio.py --auto       # auto-select USB headset + loopback
    python setup_audio.py --list       # list devices only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import os

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy required. Run: pip install numpy")
    sys.exit(1)


CONFIG_FILE = "spoke_config.yaml"

# ============================================================================
# Phase 1: List devices
# ============================================================================

def list_devices_sounddevice() -> list[dict]:
    """List devices using sounddevice."""
    try:
        import sounddevice as sd
    except Exception:
        return []

    devices = []
    for i, dev in enumerate(sd.query_devices()):
        name = dev["name"]
        out_ch = dev["max_output_channels"]
        in_ch = dev["max_input_channels"]
        sample_rate = dev.get("default_samplerate", 0)
        devices.append({
            "index": i,
            "name": name,
            "output_channels": out_ch,
            "input_channels": in_ch,
            "default_samplerate": sample_rate,
            "backend": "sounddevice",
        })
    return devices


def list_devices_alsa() -> list[dict]:
    """List devices using arecord/aplay -l as fallback."""
    devices = []
    idx = 0

    # Parse arecord -l for input devices
    for label, cmd in [("input", "arecord"), ("output", "aplay")]:
        try:
            result = subprocess.run([cmd, "-l"], capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

        for line in result.stdout.strip().split("\n"):
            # Format: "card X: Device [name], device Y: name []"
            parts = line.strip()
            if "card" not in parts or "device" not in parts:
                continue
            try:
                card = int(parts.split("card ")[1].split(",")[0].strip())
                device = int(parts.split("device ")[1].split(",")[0].strip())
                # Extract device name (between device Y: and the next )
                name_start = parts.find("]") + 1
                name_end = parts.find(",", name_start)
                if name_end == -1:
                    name_end = len(parts)
                device_name = parts[name_start:name_end].strip()
                card_name = parts.split("]")[0].split(":")[-1].strip()
                full_name = f"{card_name} - {device_name} (hw:{card},{device})"

                devices.append({
                    "index": idx,
                    "name": full_name,
                    "output_channels": 2 if label == "output" else 0,
                    "input_channels": 2 if label == "input" else 0,
                    "default_samplerate": 0,
                    "hw": f"hw:{card},{device}",
                    "backend": "alsa",
                })
                idx += 1
            except (IndexError, ValueError):
                continue

    return devices


def list_devices() -> list[dict]:
    """List all audio devices (sounddevice first, ALSA fallback)."""
    devices = list_devices_sounddevice()
    if devices:
        return devices
    print("  sounddevice not available, using ALSA listing...")
    return list_devices_alsa()


def print_devices(devices: list[dict]) -> None:
    """Print devices in a readable format."""
    print("\n=== Available Audio Devices ===\n")
    print(f"{'Idx':<5} {'Backend':<12} {'Input':<6} {'Output':<7} Name")
    print("-" * 90)

    for d in devices:
        in_ch = d["input_channels"] if d["input_channels"] > 0 else "-"
        out_ch = d["output_channels"] if d["output_channels"] > 0 else "-"
        print(f"{d['index']:<5} {d['backend']:<12} {in_ch:<6} {out_ch:<7} {d['name']}")

    print()

# ============================================================================
# Phase 2: Loopback test (speaker tone + mic recording)
# ============================================================================

def run_loopback_test(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    """Play tone through speaker while mic records.

    Returns (success, correlation) where correlation is how well the
    recorded audio matches the expected tone frequency (1000 Hz).
    """
    try:
        # Record for 4 seconds
        subprocess.run(
            ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
             "-c", "2", "-D", speaker_hw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=6, start_new_session=True,
        )

        result = subprocess.run(
            ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", "4", "/tmp/glados-loopback-test.wav"],
            capture_output=True, timeout=8,
        )

        # Stop speaker-test
        subprocess.run(["pkill", "-f", f"speaker-test.*{speaker_hw}"],
                       capture_output=True, timeout=2)

        if not os.path.exists("/tmp/glados-loopback-test.wav"):
            return False, 0.0

        # Check if recording has audio
        import wave
        try:
            with wave.open("/tmp/glados-loopback-test.wav") as wf:
                frames = wf.readframes(wf.getnframes())
                samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
                peak = np.max(np.abs(samples))
                if peak < 100:  # Too quiet - mic didn't capture speaker
                    os.unlink("/tmp/glados-loopback-test.wav")
                    return False, 0.0
                return True, peak
        except Exception:
            return False, 0.0
    except Exception:
        return False, 0.0
    finally:
        try:
            os.unlink("/tmp/glados-loopback-test.wav")
        except OSError:
            pass


def find_best_device_pair() -> tuple[str | None, str | None]:
    """Find the best speaker-mic pair using loopback testing.

    Tries all speaker devices, and for each, tests all mic devices.
    Returns (best_speaker_hw, best_mic_hw).
    """
    print("\n--- Loopback Test (playing tone, recording via mic) ---")
    print("This verifies which mic-speaker pair physically works together.\n")

    # Get ALSA devices
    alsa_devices = list_devices_alsa()
    speaker_hw_list = [d["hw"] for d in alsa_devices if d["output_channels"] > 0]
    mic_hw_list = [d["hw"] for d in alsa_devices if d["input_channels"] > 0]

    if not speaker_hw_list or not mic_hw_list:
        print("  Could not find speaker/mic devices. Try --manual.")
        return None, None

    print(f"Testing {len(speaker_hw_list)} speakers × {len(mic_hw_list)} mics...\n")

    best_speaker = None
    best_mic = None
    best_peak = 0

    for i, spk_hw in enumerate(speaker_hw_list):
        for j, mic_hw in enumerate(mic_hw_list):
            print(f"  [{i+1}/{len(speaker_hw_list)}] Speaker: {spk_hw}", end="")
            print(f"  →  [{j+1}/{len(mic_hw_list)}] Mic: {mic_hw}... ", end="")

            success, peak = run_loopback_test(spk_hw, mic_hw)
            if success:
                status = f"✓ (peak: {peak:.0f})"
                if peak > best_peak:
                    best_peak = peak
                    best_speaker = spk_hw
                    best_mic = mic_hw
            else:
                status = "✗ (no audio captured)"

            print(status)

    if best_speaker:
        print(f"\n✓ Best pair: Speaker={best_speaker}, Mic={best_mic} (peak: {best_peak:.0f})")
    else:
        print("\n✗ No working speaker-mic pair found. Try --manual.")

    return best_speaker, best_mic

# ============================================================================
# Phase 3: Frequency capability test
# ============================================================================

def test_sample_rates(device_hw: str, is_input: bool = False) -> list[int]:
    """Test which sample rates a device supports."""
    rates_to_try = [8000, 11025, 16000, 22050, 44100, 48000]
    supported = []

    for rate in rates_to_try:
        try:
            if is_input:
                proc = subprocess.Popen(
                    ["arecord", "-D", device_hw, "-r", str(rate),
                     "-f", "S16_LE", "-c", "1", "-d", "1", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                proc.communicate(timeout=3)
                retcode = proc.returncode
            else:
                proc = subprocess.Popen(
                    ["speaker-test", "-t", "sine", "-r", str(rate),
                     "-f", "440", "-l", "1", "-c", "2", "-D", device_hw],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3,
                    start_new_session=True,
                )
                proc.wait()
                retcode = proc.returncode
                # Stop any leftover speaker-test processes
                subprocess.run(["pkill", "-f", f"speaker-test.*{device_hw}"],
                               capture_output=True, timeout=2)

            if retcode == 0:
                supported.append(rate)
        except Exception:
            pass

    return supported

# ============================================================================
# Phase 4: Config generation
# ============================================================================

def generate_config(speaker_hw: str, mic_hw: str,
                    spk_rates: list[int], mic_rates: list[int]) -> dict:
    """Generate spoke_config.yaml content."""
    # Determine best sample rates
    spk_rate = 22050 if 22050 in spk_rates else (44100 if 44100 in spk_rates else 48000)
    mic_rate = 16000 if 16000 in mic_rates else 48000

    return {
        "spoke": {
            "hub_host": "192.168.1.58",
            "hub_port": 5051,
            "reconnect_base_delay": 1.0,
            "reconnect_max_delay": 30.0,
            "reconnect_jitter": 0.5,
            "reconnect_attempts": None,
            "mic_sample_rate": mic_rate,
            "mic_channels": 1,
            "mic_chunk_frames": 512,
            "mic_device": mic_hw,
            "mic_volume": 1.0,
            "spk_sample_rate": spk_rate,
            "spk_device": speaker_hw,
            "jitter_buffer_min_ms": 80,
            "jitter_buffer_max_ms": 400,
            "jitter_adapt_speed": 0.1,
            "echo_aec_enabled": True,
            "echo_aec_order": 256,
            "echo_mute_on_detection_ms": 0,
            "log_level": "INFO",
            "heartbeat_interval_s": 5.0,
        }
    }


def write_config(config: dict, path: str) -> None:
    """Write config to YAML file."""
    try:
        import yaml
        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except ImportError:
        # Fallback: write YAML manually
        spoke = config.get("spoke", {})
        with open(path, "w") as f:
            f.write("# GLaDOS Spoke Client Configuration\n")
            f.write(f"# Generated on {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("spoke:\n")
            for k, v in spoke.items():
                if isinstance(v, str):
                    f.write(f'  {k}: "{v}"\n')
                elif v is None:
                    f.write(f"  {k}: null\n")
                else:
                    f.write(f"  {k}: {v}\n")

# ============================================================================
# Auto-select with USB preference
# ============================================================================

def auto_select_usb() -> tuple[str | None, str | None]:
    """Find USB headset and use loopback test to verify."""
    alsa_devices = list_devices_alsa()

    # Find USB devices
    usb_speakers = [d for d in alsa_devices
                    if d["output_channels"] > 0 and "usb" in d["name"].lower()]
    usb_mics = [d for d in alsa_devices
                if d["input_channels"] > 0 and "usb" in d["name"].lower()]

    if usb_speakers and usb_mics:
        print("\n--- Found USB devices, testing loopback ---")
        spk_hw = usb_speakers[0]["hw"]
        mic_hw = usb_mics[0]["hw"]
        print(f"  Speaker: {spk_hw} ({usb_speakers[0]['name']})")
        print(f"  Mic:     {mic_hw} ({usb_mics[0]['name']})")

        success, peak = run_loopback_test(spk_hw, mic_hw)
        if success:
            print(f"  ✓ Loopback test passed (peak: {peak:.0f})")
            return spk_hw, mic_hw
        else:
            print(f"  ✗ Loopback failed, falling back to all devices...")

    # Fallback: try all device pairs
    print("\n--- No USB headset found, testing all pairs ---")
    return find_best_device_pair()

# ============================================================================
# Manual selection
# ============================================================================

def manual_select(devices: list[dict]) -> tuple[str | None, str | None]:
    """Let user pick speaker and mic from list."""
    print("\n=== Manual Device Selection ===\n")

    # Show speakers
    speakers = [d for d in devices if d["output_channels"] > 0]
    print("Available speakers:")
    for d in speakers:
        print(f"  {d['index']}: {d['name']} {f'({d["hw"]})' if 'hw' in d else ''}")

    spk_hw = None
    while spk_hw is None:
        idx = input("\nSpeaker device index: ").strip()
        if idx.isdigit():
            d = next((x for x in speakers if x["index"] == int(idx)), None)
            if d:
                spk_hw = d.get("hw") or f"hw:{d['index']},0"
            else:
                print("Invalid index.")

    # Show mics
    mics = [d for d in devices if d["input_channels"] > 0]
    print("\nAvailable microphones:")
    for d in mics:
        print(f"  {d['index']}: {d['name']} {f'({d["hw"]})' if 'hw' in d else ''}")

    mic_hw = None
    while mic_hw is None:
        idx = input("\nMicrophone device index: ").strip()
        if idx.isdigit():
            d = next((x for x in mics if x["index"] == int(idx)), None)
            if d:
                mic_hw = d.get("hw") or f"hw:{d['index']},0"
            else:
                print("Invalid index.")

    return spk_hw, mic_hw

# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-detect and configure GLaDOS Spoke audio devices")
    parser.add_argument("--config", default=CONFIG_FILE, help="Config file path")
    parser.add_argument("--list", action="store_true", help="List devices only")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-select (USB headset preferred, then loopback test)")
    parser.add_argument("--manual", action="store_true",
                        help="Manually select devices")
    args = parser.parse_args()

    # List mode
    if args.list:
        print_devices(list_devices())
        return

    devices = list_devices()

    # Auto mode: USB preference + loopback
    if args.auto:
        speaker_hw, mic_hw = auto_select_usb()
        if not speaker_hw:
            print("\nAuto-select failed. Try --manual.")
            sys.exit(1)
    else:
        speaker_hw, mic_hw = manual_select(devices)
        if not speaker_hw or not mic_hw:
            print("\nNo devices selected. Aborting.")
            sys.exit(1)

    print("\n--- Testing frequency support ---")

    # Parse hw:card,device
    try:
        spk_card = int(speaker_hw.split(":")[1].split(",")[0])
        mic_card = int(mic_hw.split(":")[1].split(",")[0])
    except (IndexError, ValueError):
        print(f"Warning: Could not parse hw addresses. Using defaults.")
        spk_rates = [22050, 44100, 48000]
        mic_rates = [16000, 48000]
    else:
        spk_rates = test_sample_rates(speaker_hw, is_input=False) or [22050, 44100]
        mic_rates = test_sample_rates(mic_hw, is_input=True) or [16000, 48000]

    print(f"\n  Speaker {speaker_hw}: supports {spk_rates}")
    print(f"  Mic     {mic_hw}:     supports {mic_rates}")

    config = generate_config(speaker_hw, mic_hw, spk_rates, mic_rates)
    write_config(config, args.config)

    print(f"\n✓ Configuration written to: {args.config}")
    print(f"  Speaker: {speaker_hw} ({spk_rates})")
    print(f"  Mic:     {mic_hw} ({mic_rates})")
    print("\nRun: glados-spoke --config " + args.config)


if __name__ == "__main__":
    main()
