#!/usr/bin/env python3
"""Auto-detect and configure audio devices for GLaDOS Spoke.

Two-phase detection:
1. List devices (sounddevice preferred, ALSA fallback)
2. Loopback test: play tone through speaker, mic records it
   → confirms the mic-speaker pair physically works together
3. Frequency test: check which sample rates each device supports

Requires: sounddevice (PortAudio) AND alsa-utils (arecord/aplay/speaker-test)

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


def check_prerequisites() -> None:
    """Check that required tools are installed."""
    missing = []

    # Check alsa-utils
    for cmd in ["arecord", "aplay", "speaker-test"]:
        try:
            subprocess.run([cmd, "--version"], capture_output=True, timeout=3)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            missing.append(cmd)

    if missing:
        print("ERROR: Missing ALSA utilities: " + ", ".join(missing))
        print()
        print("Install them with:")
        print("  sudo apt-get install alsa-utils")
        print()
        print("After installing, run this setup again.")
        sys.exit(1)

    # Check sounddevice
    try:
        import sounddevice as sd
        sd.query_devices()  # Force initialization to test
    except Exception as e:
        print(f"WARNING: sounddevice not working: {e}")
        print("Device listing will use ALSA fallback. For best results,")
        print("ensure PulseAudio/PipeWire is running, or install libasound2-dev.")
        print()


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
        sr = dev.get("default_samplerate", 0)
        # Extract hw address if possible
        hw = None
        if "hw:" in name:
            hw = name.split("(")[0].strip()
            # Clean up to just hw:X,Y
            parts = name.split("(")[1].rstrip(")")
            if parts.startswith("hw:"):
                hw = parts
        devices.append({
            "index": i,
            "name": name,
            "output_channels": out_ch,
            "input_channels": in_ch,
            "default_samplerate": sr,
            "hw": hw,
            "backend": "sounddevice",
        })
    return devices


def list_devices_alsa() -> list[dict]:
    """List devices using arecord/aplay -l as fallback."""
    devices = []
    idx = 0

    for label, cmd in [("input", "arecord"), ("output", "aplay")]:
        try:
            result = subprocess.run([cmd, "-l"], capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

        for line in result.stdout.strip().split("\n"):
            parts = line.strip()
            if "card" not in parts or "device" not in parts:
                continue
            try:
                card = int(parts.split("card ")[1].split(",")[0].strip())
                device = int(parts.split("device ")[1].split(",")[0].strip())
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
    print(f"{'Idx':<5} {'Backend':<12} {'In':<5} {'Out':<5} {'HW Addr':<12} Name")
    print("-" * 100)

    for d in devices:
        in_ch = str(d["input_channels"]) if d["input_channels"] > 0 else "-"
        out_ch = str(d["output_channels"]) if d["output_channels"] > 0 else "-"
        hw = d.get("hw", "-")
        if len(hw) > 12:
            hw = hw[:11] + "…"
        print(f"{d['index']:<5} {d['backend']:<12} {in_ch:<5} {out_ch:<5} {hw:<12} {d['name']}")
    print()

# ============================================================================
# Loopback test: play tone, mic records it
# ============================================================================

def run_loopback_test(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    """Play 1kHz tone through speaker, record with mic.

    Returns (success, peak_level).
    """
    output_file = "/tmp/glados-loopback-test.wav"

    try:
        # Start speaker-test in background
        spk_proc = subprocess.Popen(
            ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
             "-c", "2", "-D", speaker_hw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Record while speaker plays
        rec_proc = subprocess.Popen(
            ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", "4", output_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Wait for recording to finish
        rec_proc.wait(timeout=8)

        # Stop speaker
        spk_proc.terminate()
        try:
            spk_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            spk_proc.kill()

        # Check recording
        if not os.path.exists(output_file):
            return False, 0.0

        import wave
        with wave.open(output_file) as wf:
            frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            peak = float(np.max(np.abs(samples)))

        if peak < 100:
            return False, peak
        return True, peak

    except Exception:
        return False, 0.0
    finally:
        try:
            os.unlink(output_file)
        except OSError:
            pass


def find_best_device_pair() -> tuple[str | None, str | None]:
    """Find best speaker-mic pair via loopback testing all combinations."""
    print("\n--- Loopback Test ---")
    print("Playing tone through each speaker, recording with each mic.\n")

    alsa_devices = list_devices_alsa()
    speaker_hw_list = [d["hw"] for d in alsa_devices if d["output_channels"] > 0]
    mic_hw_list = [d["hw"] for d in alsa_devices if d["input_channels"] > 0]

    if not speaker_hw_list or not mic_hw_list:
        print("  No speaker or mic devices found.")
        return None, None

    print(f"  Testing {len(speaker_hw_list)} speakers × {len(mic_hw_list)} mics")
    print()

    best_speaker = None
    best_mic = None
    best_peak = 0

    for i, spk_hw in enumerate(speaker_hw_list):
        spk_name = next(d["name"] for d in alsa_devices if d["hw"] == spk_hw)
        print(f"  Speaker [{i+1}/{len(speaker_hw_list)}] {spk_hw}")

        for j, mic_hw in enumerate(mic_hw_list):
            mic_name = next(d["name"] for d in alsa_devices if d["hw"] == mic_hw)
            print(f"    Mic     [{j+1}/{len(mic_hw_list)}] {mic_hw}... ", end="")

            success, peak = run_loopback_test(spk_hw, mic_hw)
            if success:
                status = f"✓ (peak: {peak:.0f})"
                if peak > best_peak:
                    best_peak = peak
                    best_speaker = spk_hw
                    best_mic = mic_hw
            else:
                status = "✗ (too quiet)"
            print(status)

    print()
    if best_speaker:
        spk_name = next(d["name"] for d in alsa_devices if d["hw"] == best_speaker)
        mic_name = next(d["name"] for d in alsa_devices if d["hw"] == best_mic)
        print(f"✓ Best pair found!")
        print(f"  Speaker: {best_speaker}  ({spk_name})")
        print(f"  Mic:     {best_mic}      ({mic_name})")
        print(f"  Loopback peak: {best_peak:.0f}")
    else:
        print("✗ No working pair found. Try --manual.")

    return best_speaker, best_mic


def auto_select_usb() -> tuple[str | None, str | None]:
    """Find USB headset and verify with loopback test."""
    alsa_devices = list_devices_alsa()

    usb_speakers = [d for d in alsa_devices
                    if d["output_channels"] > 0 and "usb" in d["name"].lower()]
    usb_mics = [d for d in alsa_devices
                if d["input_channels"] > 0 and "usb" in d["name"].lower()]

    if usb_speakers and usb_mics:
        print("\n--- Found USB headset, testing loopback ---")
        spk_hw = usb_speakers[0]["hw"]
        mic_hw = usb_mics[0]["hw"]
        print(f"  Speaker: {spk_hw} ({usb_speakers[0]['name']})")
        print(f"  Mic:     {mic_hw} ({usb_mics[0]['name']})")

        success, peak = run_loopback_test(spk_hw, mic_hw)
        if success:
            print(f"  ✓ Loopback test passed (peak: {peak:.0f})")
            return spk_hw, mic_hw
        else:
            print(f"  ✗ Loopback failed, testing all pairs...")

    # Fallback: all pairs
    return find_best_device_pair()

# ============================================================================
# Frequency test
# ============================================================================

def test_sample_rates(device_hw: str, is_input: bool = False) -> list[int]:
    """Test which sample rates a device supports."""
    rates = [8000, 11025, 16000, 22050, 44100, 48000]
    supported = []

    for rate in rates:
        try:
            if is_input:
                proc = subprocess.Popen(
                    ["arecord", "-D", device_hw, "-r", str(rate),
                     "-f", "S16_LE", "-c", "1", "-d", "1", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                proc.communicate(timeout=3)
                if proc.returncode == 0:
                    supported.append(rate)
            else:
                proc = subprocess.Popen(
                    ["speaker-test", "-t", "sine", "-r", str(rate),
                     "-f", "440", "-l", "1", "-c", "2", "-D", device_hw],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3, start_new_session=True,
                )
                proc.wait()
                if proc.returncode == 0:
                    supported.append(rate)
                # Cleanup leftover processes
                subprocess.run(["pkill", "-f", f"speaker-test.*{device_hw}"],
                               capture_output=True, timeout=2)
        except Exception:
            pass

    return supported

# ============================================================================
# Config generation
# ============================================================================

def write_config(speaker_hw: str, mic_hw: str, spk_rates: list[int],
                 mic_rates: list[int], config_path: str) -> None:
    """Write spoke_config.yaml."""
    spk_rate = 22050 if 22050 in spk_rates else (44100 if 44100 in spk_rates else 48000)
    mic_rate = 16000 if 16000 in mic_rates else 48000

    config = {
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

    try:
        import yaml
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except ImportError:
        with open(config_path, "w") as f:
            f.write(f"# Generated {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("spoke:\n")
            for k, v in config["spoke"].items():
                if isinstance(v, str):
                    f.write(f'  {k}: "{v}"\n')
                elif v is None:
                    f.write(f"  {k}: null\n")
                else:
                    f.write(f"  {k}: {v}\n")

# ============================================================================
# Manual selection
# ============================================================================

def manual_select(devices: list[dict]) -> tuple[str | None, str | None]:
    """Let user pick speaker and mic from list."""
    print("\n=== Manual Device Selection ===\n")

    speakers = [d for d in devices if d["output_channels"] > 0]
    print("Speakers:")
    for d in speakers:
        hw = d.get("hw", "auto")
        print(f"  {d['index']}: {d['name']} [{hw}]")

    spk_hw = None
    while not spk_hw:
        idx = input("\nSpeaker index: ").strip()
        if idx.isdigit():
            d = next((x for x in speakers if x["index"] == int(idx)), None)
            if d:
                spk_hw = d.get("hw") or f"hw:{d['index']},0"
            else:
                print("Invalid index.")

    mics = [d for d in devices if d["input_channels"] > 0]
    print("\nMicrophones:")
    for d in mics:
        hw = d.get("hw", "auto")
        print(f"  {d['index']}: {d['name']} [{hw}]")

    mic_hw = None
    while not mic_hw:
        idx = input("\nMicrophone index: ").strip()
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

    # Check prerequisites first
    check_prerequisites()

    # List mode
    if args.list:
        print_devices(list_devices())
        return

    devices = list_devices()

    # Auto mode
    if args.auto:
        print("\n=== Auto-Detecting Audio Devices ===\n")
        speaker_hw, mic_hw = auto_select_usb()
        if not speaker_hw:
            print("\nAuto-select failed. Run: glados-spoke setup")
            sys.exit(1)
    else:
        speaker_hw, mic_hw = manual_select(devices)
        if not speaker_hw or not mic_hw:
            print("No devices selected.")
            sys.exit(1)

    print("\n--- Testing sample rate support ---")
    try:
        spk_rates = test_sample_rates(speaker_hw, is_input=False) or [22050, 44100]
        mic_rates = test_sample_rates(mic_hw, is_input=True) or [16000, 48000]
    except Exception:
        spk_rates = [22050, 44100]
        mic_rates = [16000, 48000]

    print(f"  Speaker {speaker_hw}: {spk_rates}")
    print(f"  Mic     {mic_hw}:     {mic_rates}")

    write_config(speaker_hw, mic_hw, spk_rates, mic_rates, args.config)

    print(f"\n✓ Config written to: {args.config}")
    print(f"  Speaker: {speaker_hw} (rates: {spk_rates})")
    print(f"  Mic:     {mic_hw} (rates: {mic_rates})")
    print("\nRun: glados-spoke --config " + args.config)


if __name__ == "__main__":
    main()
