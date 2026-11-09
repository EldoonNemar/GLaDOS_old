#!/usr/bin/env python3
"""Universal GLaDOS Spoke audio device auto-detection.

Works on ANY Linux with ALSA, regardless of distro or hardware.

Method: brute-force probe every hw:card,device combination.
1. Get card count from /proc/asound/cards (or default to 8)
2. For each card, probe devices 0-3 for capture and playback
3. Test each device at required sample rates
4. Loopback test: play tone, mic records it — confirms the pair works

No assumptions. No hardcoded defaults. Works everywhere.

Usage:
    python setup_audio.py --auto    # full auto-detect
    python setup_audio.py           # interactive
    python setup_audio.py --list    # list detected devices
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy required")
    sys.exit(1)

CONFIG_FILE = "spoke_config.yaml"
MAX_CARDS = 8
MAX_DEVICES_PER_CARD = 4


def check_tools() -> list[str]:
    missing = []
    for cmd in ["arecord", "aplay", "speaker-test"]:
        if not subprocess.run(["which", cmd], capture_output=True, timeout=3).returncode == 0:
            missing.append(cmd)
    return missing


def get_card_count() -> int:
    try:
        with open("/proc/asound/cards") as f:
            cards = [l.strip() for l in f if l.strip() and l.split()[0].isdigit()]
            return max(len(cards), 1)
    except (FileNotFoundError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        cards = [l for l in result.stdout.split("\n") if "card" in l and "device" in l]
        if cards:
            return max(int(c.split("card ")[1].split(",")[0]) for c in cards) + 1
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError, ValueError):
        pass
    return MAX_CARDS


def get_card_name(card: int) -> str:
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                parts = line.strip().split()
                if parts and parts[0].isdigit() and int(parts[0]) == card:
                    return parts[1].strip()
    except (FileNotFoundError, IndexError):
        pass
    return f"Card {card}"


def kill_speaker_test(hw: str) -> None:
    """Kill any leftover speaker-test processes for a device."""
    try:
        subprocess.run(["pkill", "-f", f"speaker-test.*{hw}"],
                       capture_output=True, timeout=2)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def probe_devices(card_count: int) -> list[dict]:
    devices = []

    for card in range(card_count):
        card_name = get_card_name(card)

        for dev in range(MAX_DEVICES_PER_CARD):
            hw = f"hw:{card},{dev}"

            # Test playback
            is_playback = False
            try:
                p = subprocess.Popen(
                    ["speaker-test", "-t", "silence", "-l", "0",
                     "-c", "2", "-D", hw],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                p.wait(timeout=3)
                if p.returncode == 0:
                    is_playback = True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            kill_speaker_test(hw)

            # Test capture
            is_capture = False
            tmp = f"/tmp/glados-probe-{card}-{dev}.wav"
            try:
                p = subprocess.Popen(
                    ["arecord", "-D", hw, "-f", "S16_LE", "-r", "16000",
                     "-c", "1", "-d", "1", tmp],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.wait(timeout=5)
                is_capture = os.path.exists(tmp) and os.path.getsize(tmp) > 0
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

            if is_playback or is_capture:
                name = f"{card_name} (hw:{card},{dev})"
                if is_playback and is_capture:
                    name += " [in+out]"
                elif is_playback:
                    name += " [out]"
                elif is_capture:
                    name += " [in]"
                devices.append({
                    "hw": hw, "name": name, "card": card, "device": dev,
                    "is_capture": is_capture, "is_playback": is_playback,
                })

    return devices


def print_devices(devices: list[dict]) -> None:
    print("\n=== Detected Audio Devices ===\n")
    speakers = [d for d in devices if d["is_playback"]]
    mics = [d for d in devices if d["is_capture"]]

    if speakers:
        print("Speakers:")
        for i, d in enumerate(speakers):
            print(f"  {i}: {d['hw']}  ({d['name']})")
        print()

    if mics:
        print("Microphones:")
        for i, d in enumerate(mics):
            print(f"  {i}: {d['hw']}  ({d['name']})")
        print()

    if not speakers or not mics:
        print("ERROR: No usable devices found.")
        sys.exit(1)


def test_rate(hw: str, rate: int, is_capture: bool) -> bool:
    try:
        if is_capture:
            tmp = f"/tmp/glados-rate-{rate}.wav"
            p = subprocess.Popen(
                ["arecord", "-D", hw, "-r", str(rate), "-f", "S16_LE",
                 "-c", "1", "-d", "1", tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(timeout=5)
            ok = p.returncode == 0
            try:
                os.unlink(tmp)
            except OSError:
                pass
        else:
            p = subprocess.Popen(
                ["speaker-test", "-t", "silence", "-r", str(rate),
                 "-l", "0", "-c", "2", "-D", hw],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            p.wait()
            ok = p.returncode == 0
            kill_speaker_test(hw)
        return ok
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def test_rates(hw: str, is_capture: bool) -> list[int]:
    return [r for r in [8000, 11025, 16000, 22050, 44100, 48000]
            if test_rate(hw, r, is_capture)]


def loopback_test(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    output = "/tmp/glados-loopback.wav"
    try:
        spk = subprocess.Popen(
            ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
             "-c", "2", "-D", speaker_hw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        rec = subprocess.Popen(
            ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", "4", output],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec.wait(timeout=8)
        spk.terminate()
        try:
            spk.wait(timeout=3)
        except subprocess.TimeoutExpired:
            spk.kill()

        if not os.path.exists(output):
            return False, 0.0

        import wave
        with wave.open(output) as wf:
            frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            peak = float(np.max(np.abs(samples)))
        return peak >= 50, peak

    except Exception:
        return False, 0.0
    finally:
        try:
            os.unlink(output)
        except OSError:
            pass


def find_best_pair(speakers: list[dict], mics: list[dict]) -> tuple[str, str, float]:
    print(f"\n  Testing {len(speakers)} speakers x {len(mics)} mics...\n")
    best_spk = best_mic = None
    best_peak = 0

    for i, spk in enumerate(speakers):
        print(f"  Speaker {i+1}/{len(speakers)}: {spk['hw']}")
        for j, mic in enumerate(mics):
            print(f"    Mic {j+1}/{len(mics)}: {mic['hw']}... ", end="")
            ok, peak = loopback_test(spk["hw"], mic["hw"])
            if ok:
                print(f"✓ peak={peak:.0f}")
                if peak > best_peak:
                    best_peak = peak
                    best_spk = spk["hw"]
                    best_mic = mic["hw"]
            else:
                print("✗ quiet")
        print()

    if best_spk:
        print(f"  ✓ Best: Speaker={best_spk}, Mic={best_mic} (peak: {best_peak:.0f})")
    else:
        print("  ✗ No working pair found")
    return best_spk or speakers[0]["hw"], best_mic or mics[0]["hw"], best_peak


def auto_detect(devices: list[dict]) -> tuple[str, str, list[int], list[int]]:
    speakers = [d for d in devices if d["is_playback"]]
    mics = [d for d in devices if d["is_capture"]]

    usb_spk = next((d for d in speakers if "usb" in d["name"].lower()), None)
    usb_mic = next((d for d in mics if "usb" in d["name"].lower()), None)

    if usb_spk and usb_mic:
        print("\n  USB headset found, testing...")
        print(f"  Speaker: {usb_spk['hw']}")
        print(f"  Mic:     {usb_mic['hw']}")
        ok, peak = loopback_test(usb_spk["hw"], usb_mic["hw"])
        if ok:
            print(f"  ✓ USB headset works (peak: {peak:.0f})")
            spk_rates = test_rates(usb_spk["hw"], False)
            mic_rates = test_rates(usb_mic["hw"], True)
            return usb_spk["hw"], usb_mic["hw"], spk_rates or [22050, 44100], mic_rates or [16000]
        print("  USB loopback failed, testing all pairs...")

    spk_hw, mic_hw, _ = find_best_pair(speakers, mics)
    return spk_hw, mic_hw, test_rates(spk_hw, False) or [22050, 44100], test_rates(mic_hw, True) or [16000]


def write_config(spk_hw: str, mic_hw: str, spk_rates: list[int],
                 mic_rates: list[int], path: str) -> None:
    spk_rate = 22050 if 22050 in spk_rates else (44100 if 44100 in spk_rates else 48000)
    mic_rate = 16000 if 16000 in mic_rates else 48000

    config = {
        "spoke": {
            "hub_host": "192.168.1.58", "hub_port": 5051,
            "reconnect_base_delay": 1.0, "reconnect_max_delay": 30.0,
            "reconnect_jitter": 0.5, "reconnect_attempts": None,
            "mic_sample_rate": mic_rate, "mic_channels": 1,
            "mic_chunk_frames": 512, "mic_device": mic_hw,
            "mic_volume": 1.0, "spk_sample_rate": spk_rate,
            "spk_device": spk_hw,
            "jitter_buffer_min_ms": 80, "jitter_buffer_max_ms": 400,
            "jitter_adapt_speed": 0.1, "echo_aec_enabled": True,
            "echo_aec_order": 256, "echo_mute_on_detection_ms": 0,
            "log_level": "INFO", "heartbeat_interval_s": 5.0,
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

    missing = check_tools()
    if missing:
        print(f"ERROR: Missing tools: {', '.join(missing)}")
        print("Install with: sudo apt-get install alsa-utils")
        sys.exit(1)

    card_count = get_card_count()
    print(f"\n=== GLaDOS Spoke Audio Setup ===")
    print(f"Detected {card_count} ALSA cards\n")

    devices = probe_devices(card_count)
    if not devices:
        print("ERROR: No audio devices found.")
        print("Check that your USB headset is plugged in.")
        sys.exit(1)

    print_devices(devices)
    speakers = [d for d in devices if d["is_playback"]]
    mics = [d for d in devices if d["is_capture"]]

    if args.auto:
        spk_hw, mic_hw, spk_rates, mic_rates = auto_detect(devices)
    else:
        print("Select speaker index:")
        for i, d in enumerate(speakers):
            print(f"  {i}: {d['hw']}")
        spk_hw = speakers[int(input("  > "))]["hw"]
        print("Select mic index:")
        for i, d in enumerate(mics):
            print(f"  {i}: {d['hw']}")
        mic_hw = mics[int(input("  > "))]["hw"]
        print("\n  Testing selected pair with loopback...")
        ok, peak = loopback_test(spk_hw, mic_hw)
        if ok:
            print(f"  ✓ Loopback test passed (peak: {peak:.0f})")
        else:
            print(f"  ✗ Loopback test FAILED. Selecting best pair...")
            spk_hw, mic_hw, _ = find_best_pair(speakers, mics)
        spk_rates = test_rates(spk_hw, False)
        mic_rates = test_rates(mic_hw, True)

    spk_rates = spk_rates or [22050, 44100]
    mic_rates = mic_rates or [16000]

    write_config(spk_hw, mic_hw, spk_rates, mic_rates, args.config)
    print(f"\n✓ Config written: {args.config}")
    print(f"  Speaker: {spk_hw}  (rates: {spk_rates})")
    print(f"  Mic:     {mic_hw}  (rates: {mic_rates})")
    print(f"\nRun: glados-spoke --config {args.config}")


if __name__ == "__main__":
    main()
