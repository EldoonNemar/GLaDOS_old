#!/usr/bin/env python3
"""Auto-detect and configure audio devices for GLaDOS Spoke.

Uses sounddevice to find working devices, test them, and update
spoke_config.yaml automatically. Much simpler than ALSA parsing.

Usage:
    python setup_audio.py              # interactive setup
    python setup_audio.py --auto       # auto-select best USB headset
    python setup_audio.py --list       # list devices only
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import sounddevice as sd
    import numpy as np
except ImportError:
    print("ERROR: sounddevice is required. Install it with:")
    print("  pip install -e .")
    sys.exit(1)


# Sample rates we need to test
MIC_RATE = 16000
SPK_RATE = 22050


def list_devices() -> None:
    """List all available audio devices."""
    print("\n=== Available Audio Devices ===\n")

    devices = sd.query_devices()

    print(f"{'Index':<6} {'Name'}")
    print("-" * 80)

    for i, dev in enumerate(devices):
        name = dev["name"]
        out_ch = dev["max_output_channels"]
        in_ch = dev["max_input_channels"]
        tags = []
        if out_ch > 0:
            tags.append(f"out:{out_ch}")
        if in_ch > 0:
            tags.append(f"in:{in_ch}")
        print(f"{i:<6} {name}  [{', '.join(tags)}]")

    print(f"\nDefault: {sd.default.device}")
    print()


def test_output_device(index: int) -> tuple[bool, list[int]]:
    """Test if an output device works at various sample rates.

    Returns (success, working_rates) where working_rates is the list of
    sample rates that the device supports.
    """
    working_rates = []
    # Try common sample rates
    for rate in [22050, 44100, 48000, 16000, 8000]:
        try:
            duration = 0.3
            t = np.linspace(0, duration, int(rate * duration), endpoint=False)
            tone = np.sin(440 * 2 * np.pi * t).astype(np.float32)
            sd.play(tone, rate, device=(index, None))
            sd.sleep(int(duration * 1000))
            sd.stop()
            working_rates.append(rate)
        except Exception:
            pass

    success = SPK_RATE in working_rates
    if success:
        print(f"  ✓ Device {index} works at {SPK_RATE} Hz")
    else:
        print(f"  ✗ Device {index} does NOT support {SPK_RATE} Hz")
    return success, working_rates


def test_input_device(index: int) -> bool:
    """Test if an input device works at the mic sample rate.

    Records 1 second and checks if we got any audio.
    """
    try:
        # Record 1 second
        rec = sd.rec(int(MIC_RATE * 1.5), samplerate=MIC_RATE, channels=1,
                     dtype=np.float32, device=(None, index))
        sd.wait()
        peak = np.max(np.abs(rec))
        if peak > 0.001:
            print(f"  ✓ Device {index} works at {MIC_RATE} Hz (peak: {peak:.3f})")
            return True
        else:
            print(f"  ⚠ Device {index} recorded but very quiet (peak: {peak:.6f})")
            return True  # Still works, just quiet
    except Exception as e:
        print(f"  ✗ Device {index} failed: {e}")
        return False


def find_usb_device(devices: list, pattern: str = "usb") -> int | None:
    """Find the first device whose name contains the pattern (case-insensitive)."""
    for i, dev in enumerate(devices):
        if pattern.lower() in dev["name"].lower():
            return i
    return None


def auto_select() -> tuple[int | None, int | None]:
    """Automatically select the best speaker and mic devices.

    Prefers USB headsets. Falls back to system default.
    """
    devices = sd.query_devices()
    print("\n=== Auto-Selecting Devices ===\n")

    # Find USB audio device for speaker
    usb_spk = find_usb_device(devices, "usb")
    if usb_spk:
        print(f"Found USB device: device {usb_spk} = {devices[usb_spk]['name']}")
        success, rates = test_output_device(usb_spk)
        if success:
            spk_device = usb_spk
        else:
            # Try the next USB device
            spk_device = None
    else:
        spk_device = None
        print("No USB device found. Trying default output device...")

    if spk_device is None:
        # Try default device
        default_dev = sd.default.device
        if default_dev[1] is not None:
            spk_device = default_dev[1]
            print(f"Using default output device {spk_device}")
            success, _ = test_output_device(spk_device)
            if not success:
                print("Default output device doesn't work. Try --manual.")
                spk_device = None

    # Find USB audio device for mic
    usb_mic = find_usb_device(devices, "usb")
    if usb_mic and usb_mic != spk_device:
        print(f"Using same USB device {usb_mic} for microphone")
        mic_device = usb_mic
    else:
        default_dev = sd.default.device
        if default_dev[0] is not None:
            mic_device = default_dev[0]
            print(f"Using default input device {mic_device}")
        else:
            mic_device = None
            print("No default input device found. Try --manual.")

    # Test mic
    if mic_device is not None:
        test_input_device(mic_device)

    return spk_device, mic_device


def manual_select() -> tuple[int | None, int | None]:
    """Let user manually select devices."""
    devices = sd.query_devices()
    print("\n=== Manual Device Selection ===\n")

    print("Output devices (speaker):")
    out_indices = [i for i, d in enumerate(devices) if d["max_output_channels"] > 0]
    for i in out_indices:
        print(f"  {i}: {devices[i]['name']}")

    spk_device = None
    while spk_device is None:
        try:
            val = input("\nSelect speaker device index (or Enter for default): ").strip()
            if val == "":
                default_dev = sd.default.device
                spk_device = default_dev[1] if default_dev[1] is not None else None
                if spk_device is None:
                    print("No default output device. Please enter an index.")
                    continue
                print(f"Using default output device {spk_device}")
            else:
                idx = int(val)
                if idx in out_indices:
                    spk_device = idx
                    success, _ = test_output_device(idx)
                    if not success:
                        print("This device doesn't support the required sample rate. Try another.")
                        spk_device = None
                else:
                    print(f"Invalid index. Choose from: {out_indices}")
        except ValueError:
            print("Please enter a number.")

    print("\nInput devices (microphone):")
    in_indices = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    for i in in_indices:
        print(f"  {i}: {devices[i]['name']}")

    mic_device = None
    while mic_device is None:
        try:
            val = input("\nSelect mic device index (or Enter for default): ").strip()
            if val == "":
                default_dev = sd.default.device
                mic_device = default_dev[0] if default_dev[0] is not None else None
                if mic_device is None:
                    print("No default input device. Please enter an index.")
                    continue
                print(f"Using default input device {mic_device}")
            else:
                idx = int(val)
                if idx in in_indices:
                    mic_device = idx
                    test_input_device(idx)
                else:
                    print(f"Invalid index. Choose from: {in_indices}")
        except ValueError:
            print("Please enter a number.")

    return spk_device, mic_device


def update_config(config_path: str, spk_device: int | None, mic_device: str | None) -> None:
    """Update spoke_config.yaml with the selected devices."""
    import yaml

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        config = {}

    spoke = config.setdefault("spoke", {})

    # Update speaker device (just the card number)
    if spk_device is not None:
        spoke["spk_device"] = spk_device

    # Update mic device (as hw:card,device string)
    if mic_device is not None:
        spoke["mic_device"] = f"hw:{mic_device},0"

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\nConfiguration written to: {config_path}")
    print(f"  Speaker device: {spk_device}")
    print(f"  Mic device: hw:{mic_device},0")


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-detect and configure GLaDOS Spoke audio devices")
    parser.add_argument("--config", default="spoke_config.yaml", help="Path to config file")
    parser.add_argument("--list", action="store_true", help="List devices and exit")
    parser.add_argument("--auto", action="store_true", help="Auto-select best devices (USB preferred)")
    parser.add_argument("--manual", action="store_true", help="Manually select devices")
    args = parser.parse_args()

    if args.list:
        list_devices()
        return

    if args.auto:
        spk_device, mic_device = auto_select()
    else:
        spk_device, mic_device = manual_select()

    if spk_device is not None:
        update_config(args.config, spk_device, mic_device)
        print("\n=== Setup Complete ===")
        print(f"Run: glados-spoke --config {args.config}")
    else:
        print("\nERROR: Could not find a working speaker device.")
        print("Try --list to see available devices, or --manual to pick one.")
        sys.exit(1)


if __name__ == "__main__":
    main()
