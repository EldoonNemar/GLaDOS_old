#!/usr/bin/env python3
"""Auto-detect and configure GLaDOS Spoke audio devices.

Works on any Linux with ALSA, even when arecord/aplay -l returns empty.
Uses direct device probing instead of listing.

Requires: alsa-utils (arecord, aplay, speaker-test)

Usage:
    python setup_audio.py --auto
    python setup_audio.py
    python setup_audio.py --list
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import os
import glob

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy required. Run: pip install numpy")
    sys.exit(1)

CONFIG_FILE = "spoke_config.yaml"


def check_prerequisites() -> None:
    """Check required tools."""
    for cmd in ["arecord", "aplay", "speaker-test"]:
        try:
            subprocess.run([cmd, "--version"], capture_output=True, timeout=3)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print(f"ERROR: {cmd} not found. Install alsa-utils:")
            print("  sudo apt-get install alsa-utils")
            sys.exit(1)


def find_alsa_devices() -> list[dict]:
    """Find ALSA devices by probing /dev/snd/ directly.

    Works even when arecord/aplay -l returns empty (common on ARM boards).
    """
    devices = []
    snd_path = "/dev/snd"

    if not os.path.isdir(snd_path):
        return devices

    # Parse /proc/asound/cards for card names
    card_names = {}
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].isdigit():
                    card_names[int(parts[0])] = parts[1].strip()
    except (FileNotFoundError, ValueError):
        pass

    # Scan /dev/snd/ for pcm devices
    pcm_files = sorted(glob.glob(f"{snd_path}/pcmC*D*p") + glob.glob(f"{snd_path}/pcmC*D*c"))

    for f in pcm_files:
        basename = os.path.basename(f)
        # Format: pcmCXDYp (playback) or pcmCXDYc (capture)
        try:
            # Extract card and device numbers
            card_match = basename[3:]
            card = int(card_match.split("D")[0])
            rest = card_match.split("D")[1]
            device = int(rest[0])
            is_capture = "c" in rest
            is_playback = "p" in rest

            name = card_names.get(card, f"Card {card}")
            dev_type = "capture" if is_capture else "playback"

            devices.append({
                "hw": f"hw:{card},{device}",
                "name": f"{name} ({dev_type} {card},{device})",
                "card": card,
                "device": device,
                "is_capture": is_capture,
                "is_playback": is_playback,
                "backend": "alsa-probe",
            })
        except (ValueError, IndexError):
            continue

    # Also try arecord -l and aplay -l
    for label, cmd in [("input", "arecord"), ("output", "aplay")]:
        try:
            result = subprocess.run([cmd, "-l"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split("\n"):
                if "card" not in line or "device" not in line:
                    continue
                try:
                    card = int(line.split("card ")[1].split(",")[0].strip())
                    device = int(line.split("device ")[1].split(",")[0].strip())
                    name_start = line.find("]") + 1
                    name_end = line.find(",", name_start)
                    if name_end == -1:
                        name_end = len(line)
                    dev_name = line[name_start:name_end].strip()
                    card_name = line.split("]")[0].split(":")[-1].strip()
                    full_name = f"{card_name} - {dev_name} (hw:{card},{device})"

                    devices.append({
                        "hw": f"hw:{card},{device}",
                        "name": full_name,
                        "card": card,
                        "device": device,
                        "is_capture": label == "input",
                        "is_playback": label == "output",
                        "backend": "alsa",
                    })
                except (IndexError, ValueError):
                    continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # Deduplicate by hw address
    seen = set()
    unique = []
    for d in devices:
        if d["hw"] not in seen:
            seen.add(d["hw"])
            unique.append(d)

    return unique


def list_devices() -> list[dict]:
    """List all audio devices."""
    try:
        import sounddevice as sd
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            hw = None
            if "hw:" in dev["name"]:
                start = dev["name"].find("(") + 1
                end = dev["name"].find(")", start)
                if start > 0 and end > start:
                    hw = dev["name"][start:end]
            devices.append({
                "hw": hw,
                "name": dev["name"],
                "is_capture": dev["max_input_channels"] > 0,
                "is_playback": dev["max_output_channels"] > 0,
                "backend": "sounddevice",
            })
        if devices:
            return devices
    except Exception:
        pass

    print("  Using ALSA device probe...")
    return find_alsa_devices()


def print_devices(devices: list[dict]) -> None:
    """Print devices."""
    print("\n=== Audio Devices ===\n")
    print(f"{'Idx':<5} {'In':<5} {'Out':<5} Name")
    print("-" * 80)
    for d in devices:
        in_ch = "Y" if d["is_capture"] else "-"
        out_ch = "Y" if d["is_playback"] else "-"
        print(f"{devices.index(d):<5} {in_ch:<5} {out_ch:<5} {d['name']}")
    print()


def run_loopback_test(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    """Play 1kHz tone through speaker, record with mic."""
    output_file = "/tmp/glados-loopback.wav"

    try:
        # Start speaker-test
        spk_proc = subprocess.Popen(
            ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
             "-c", "2", "-D", speaker_hw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Record
        rec_proc = subprocess.Popen(
            ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", "4", output_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        rec_proc.wait(timeout=8)

        spk_proc.terminate()
        try:
            spk_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            spk_proc.kill()

        if not os.path.exists(output_file):
            return False, 0.0

        import wave
        with wave.open(output_file) as wf:
            frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            peak = float(np.max(np.abs(samples)))

        return peak >= 100, peak

    except Exception:
        return False, 0.0
    finally:
        try:
            os.unlink(output_file)
        except OSError:
            pass


def find_best_pair(devices: list[dict]) -> tuple[str | None, str | None]:
    """Loopback test all speaker-mic pairs."""
    speakers = [d for d in devices if d["is_playback"]]
    mics = [d for d in devices if d["is_capture"]]

    if not speakers or not mics:
        print("  No playback or capture devices found.")
        return None, None

    print(f"\n  Testing {len(speakers)} speakers × {len(mics)} mics...\n")

    best_spk = None
    best_mic = None
    best_peak = 0

    for spk in speakers:
        print(f"  Speaker: {spk['hw']} ({spk['name']})")
        for mic in mics:
            print(f"    Mic: {mic['hw']} ({mic['name']})... ", end="")
            success, peak = run_loopback_test(spk["hw"], mic["hw"])
            if success:
                print(f"✓ peak: {peak:.0f}")
                if peak > best_peak:
                    best_peak = peak
                    best_spk = spk["hw"]
                    best_mic = mic["hw"]
            else:
                print("✗ quiet")
        print()

    if best_spk:
        print(f"✓ Best: Speaker={best_spk}, Mic={best_mic} (peak: {best_peak:.0f})")
    else:
        print("✗ No working pair found")

    return best_spk, best_mic


def auto_select(devices: list[dict]) -> tuple[str | None, str | None]:
    """Auto-select: prefer USB, then all pairs."""
    print("\n=== Auto-Detect ===\n")

    usb_spk = next((d for d in devices if d["is_playback"] and "usb" in d["name"].lower()), None)
    usb_mic = next((d for d in devices if d["is_capture"] and "usb" in d["name"].lower()), None)

    if usb_spk and usb_mic:
        print(f"USB headset found: {usb_spk['name']}, {usb_mic['name']}")
        print("Testing loopback...")
        success, peak = run_loopback_test(usb_spk["hw"], usb_mic["hw"])
        if success:
            print(f"✓ Works! (peak: {peak:.0f})")
            return usb_spk["hw"], usb_mic["hw"]
        print("USB loopback failed, testing all pairs...")

    return find_best_pair(devices)


def test_rates(hw: str, is_input: bool) -> list[int]:
    """Test sample rates."""
    rates = [8000, 16000, 22050, 44100, 48000]
    supported = []

    for rate in rates:
        try:
            if is_input:
                p = subprocess.Popen(
                    ["arecord", "-D", hw, "-r", str(rate), "-f", "S16_LE",
                     "-c", "1", "-d", "1", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                p.communicate(timeout=3)
                if p.returncode == 0:
                    supported.append(rate)
            else:
                p = subprocess.Popen(
                    ["speaker-test", "-t", "sine", "-r", str(rate),
                     "-f", "440", "-l", "1", "-c", "2", "-D", hw],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3, start_new_session=True)
                p.wait()
                if p.returncode == 0:
                    supported.append(rate)
                subprocess.run(["pkill", "-f", f"speaker-test.*{hw}"],
                               capture_output=True, timeout=2)
        except Exception:
            pass

    return supported


def write_config(spk_hw: str, mic_hw: str, spk_rates: list[int],
                 mic_rates: list[int], path: str) -> None:
    """Write config."""
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
            "spk_device": spk_hw,
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
        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    except ImportError:
        with open(path, "w") as f:
            f.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("spoke:\n")
            for k, v in config["spoke"].items():
                if isinstance(v, str):
                    f.write(f'  {k}: "{v}"\n')
                elif v is None:
                    f.write(f"  {k}: null\n")
                else:
                    f.write(f"  {k}: {v}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="GLaDOS Spoke audio setup")
    parser.add_argument("--config", default=CONFIG_FILE)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    check_prerequisites()

    if args.list:
        print_devices(list_devices())
        return

    devices = list_devices()
    if not devices:
        print("ERROR: No audio devices found.")
        print("Check that your USB headset is plugged in and recognized:")
        print("  cat /proc/asound/cards")
        print("  ls /dev/snd/")
        sys.exit(1)

    print_devices(devices)

    if args.auto:
        spk_hw, mic_hw = auto_select(devices)
    else:
        speakers = [d for d in devices if d["is_playback"]]
        mics = [d for d in devices if d["is_capture"]]
        print("Speakers:")
        for i, d in enumerate(speakers):
            print(f"  {i}: {d['hw']} ({d['name']})")
        print("\nMicrophones:")
        for i, d in enumerate(mics):
            print(f"  {i}: {d['hw']} ({d['name']})")
        spk_idx = int(input("\nSpeaker index: "))
        mic_idx = int(input("Mic index: "))
        spk_hw = speakers[spk_idx]["hw"]
        mic_hw = mics[mic_idx]["hw"]

    if not spk_hw or not mic_hw:
        print("No devices selected.")
        sys.exit(1)

    print("\n--- Sample rate test ---")
    try:
        spk_rates = test_rates(spk_hw, False) or [22050, 44100]
        mic_rates = test_rates(mic_hw, True) or [16000, 48000]
    except Exception:
        spk_rates, mic_rates = [22050, 44100], [16000, 48000]

    print(f"  Speaker {spk_hw}: {spk_rates}")
    print(f"  Mic     {mic_hw}: {mic_rates}")

    write_config(spk_hw, mic_hw, spk_rates, mic_rates, args.config)
    print(f"\n✓ Config: {args.config}")
    print(f"  Speaker: {spk_hw} ({spk_rates})")
    print(f"  Mic:     {mic_hw} ({mic_rates})")
    print(f"\nRun: glados-spoke --config {args.config}")


if __name__ == "__main__":
    main()
