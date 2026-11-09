#!/usr/bin/env python3
"""Smart GLaDOS Spoke audio device auto-detection.

Two-phase detection (mirrors human hearing):
1. FIND THE MIC: Probe all capture devices, pick the best one
   - USB headset preferred
   - Falls back to highest ambient sound level
2. FIND THE SPEAKER: Play tone through each playback device,
   record with the chosen mic. The device the mic hears is the answer.
3. LOOPBACK VERIFY: Confirm the pair works together.

No brute-force all-pairs. No assumptions. Fast and reliable.

Usage:
    python setup_audio.py --auto
    python setup_audio.py
    python setup_audio.py --list
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
    try:
        subprocess.run(["pkill", "-f", f"speaker-test.*{hw}"],
                       capture_output=True, timeout=2)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def find_capture_devices(card_count: int) -> list[dict]:
    devices = []
    for card in range(card_count):
        card_name = get_card_name(card)
        for dev in range(MAX_DEVICES_PER_CARD):
            hw = f"hw:{card},{dev}"
            tmp = f"/tmp/glados-capture-test.wav"
            try:
                p = subprocess.Popen(
                    ["arecord", "-D", hw, "-f", "S16_LE", "-r", "16000",
                     "-c", "1", "-d", "1", tmp],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.wait(timeout=5)
                if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    with open(tmp, "rb") as f:
                        f.read(44)
                        raw = f.read()
                    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                    peak = float(np.max(np.abs(samples)))
                    devices.append({
                        "hw": hw,
                        "name": f"{card_name} (hw:{card},{dev}) [in]",
                        "card": card,
                        "device": dev,
                        "is_capture": True,
                        "is_playback": False,
                        "ambient_peak": peak,
                    })
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return devices


def find_playback_devices(card_count: int) -> list[dict]:
    devices = []
    for card in range(card_count):
        card_name = get_card_name(card)
        for dev in range(MAX_DEVICES_PER_CARD):
            hw = f"hw:{card},{dev}"
            try:
                p = subprocess.Popen(
                    ["speaker-test", "-t", "silence", "-l", "0",
                     "-c", "2", "-D", hw],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                p.wait(timeout=3)
                if p.returncode == 0:
                    devices.append({
                        "hw": hw,
                        "name": f"{card_name} (hw:{card},{dev}) [out]",
                        "card": card,
                        "device": dev,
                        "is_capture": False,
                        "is_playback": True,
                    })
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            kill_speaker_test(hw)
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


def select_mic(mics: list[dict]) -> str:
    print("\n=== Finding Microphone ===")

    usb_mic = next((m for m in mics if "usb" in m["name"].lower()), None)
    if usb_mic:
        print(f"  Found USB mic: {usb_mic['hw']}")
        print("  Verifying...")
        tmp = f"/tmp/glados-mic-test.wav"
        try:
            p = subprocess.Popen(
                ["arecord", "-D", usb_mic['hw'], "-f", "S16_LE", "-r", "16000",
                 "-c", "1", "-d", "2", tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(timeout=5)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                with open(tmp, "rb") as f:
                    f.read(44)
                    raw = f.read()
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                peak = float(np.max(np.abs(samples)))
                os.unlink(tmp)
                if peak > 10:
                    print(f"  USB mic works (ambient peak: {peak:.0f})")
                    return usb_mic['hw']
                print(f"  USB mic too quiet (peak: {peak:.0f}), trying others...")
            else:
                print("  USB mic recorded nothing")
        except Exception as e:
            print(f"  USB mic error: {e}")

    if len(mics) > 1:
        mics_sorted = sorted(mics, key=lambda m: m.get("ambient_peak", 0), reverse=True)
        peaks = [f"{m['ambient_peak']:.0f}" for m in mics_sorted]
        print(f"  Ambient levels: {peaks}")
        best = mics_sorted[0]
        print(f"  Selected: {best['hw']} (peak: {best['ambient_peak']:.0f})")
    else:
        best = mics[0]
        print(f"  Selected: {best['hw']}")

    return best['hw']


def select_speaker(speakers: list[dict], mic_hw: str) -> str:
    print("\n=== Finding Speaker ===")
    print("  Playing 1kHz tone through each speaker while mic records...\n")

    best_spk = None
    best_peak = 0

    for i, spk in enumerate(speakers):
        hw = spk["hw"]
        print(f"  Speaker {i+1}/{len(speakers)}: {hw}", end="")

        output = f"/tmp/glados-spk-test-{i}.wav"
        try:
            spk_proc = subprocess.Popen(
                ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
                 "-c", "2", "-D", hw],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)

            rec_proc = subprocess.Popen(
                ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
                 "-c", "1", "-d", "3", output],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rec_proc.wait(timeout=8)

            spk_proc.terminate()
            try:
                spk_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                spk_proc.kill()

            if os.path.exists(output) and os.path.getsize(output) > 0:
                with open(output, "rb") as f:
                    f.read(44)
                    raw = f.read()
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                peak = float(np.max(np.abs(samples)))

                if peak > 50:
                    print(f"  heard by mic (peak: {peak:.0f})")
                    if peak > best_peak:
                        best_peak = peak
                        best_spk = hw
                else:
                    print(f"  too quiet (peak: {peak:.0f})")
            else:
                print("  no recording")

        except Exception as e:
            print(f"  error: {e}")
        finally:
            try:
                os.unlink(output)
            except OSError:
                pass

    if best_spk:
        print(f"\n  Speaker found: {best_spk} (peak: {best_peak:.0f})")
    else:
        print("\n  No speaker detected by mic.")
        print("  Try interactive mode: glados-spoke setup")
        sys.exit(1)

    return best_spk


def loopback_verify(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    output = "/tmp/glados-loopback-verify.wav"
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

        with open(output, "rb") as f:
            f.read(44)
            raw = f.read()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        peak = float(np.max(np.abs(samples)))
        return peak >= 50, peak

    except Exception:
        return False, 0.0
    finally:
        try:
            os.unlink(output)
        except OSError:
            pass


def test_rates(hw: str, is_capture: bool) -> list[int]:
    rates = []
    for rate in [8000, 16000, 22050, 44100, 48000]:
        try:
            if is_capture:
                tmp = f"/tmp/glados-rate-{rate}.wav"
                p = subprocess.Popen(
                    ["arecord", "-D", hw, "-r", str(rate), "-f", "S16_LE",
                     "-c", "1", "-d", "1", tmp],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.wait(timeout=5)
                if p.returncode == 0:
                    rates.append(rate)
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
                if p.returncode == 0:
                    rates.append(rate)
                kill_speaker_test(hw)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return rates


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

    capture_devices = find_capture_devices(card_count)
    playback_devices = find_playback_devices(card_count)

    if not capture_devices or not playback_devices:
        print("ERROR: No usable audio devices found.")
        print("Check that your USB headset is plugged in and recognized.")
        sys.exit(1)

    devices = capture_devices + playback_devices
    print_devices(devices)

    if args.auto:
        mic_hw = select_mic(capture_devices)
        spk_hw = select_speaker(playback_devices, mic_hw)
        print("\n=== Verifying Pair ===")
        ok, peak = loopback_verify(spk_hw, mic_hw)
        if ok:
            print(f"  Verified (peak: {peak:.0f})")
        else:
            print("  Verification failed. The mic didn't hear the speaker.")
            print("  Try interactive mode: glados-spoke setup")
            sys.exit(1)
    else:
        speakers = [d for d in devices if d["is_playback"]]
        mics = [d for d in devices if d["is_capture"]]
        print("Select speaker index:")
        for i, d in enumerate(speakers):
            print(f"  {i}: {d['hw']}")
        spk_hw = speakers[int(input("  > "))]["hw"]
        print("Select mic index:")
        for i, d in enumerate(mics):
            print(f"  {i}: {d['hw']}")
        mic_hw = mics[int(input("  > "))]["hw"]
        print("\n  Testing selected pair with loopback...")
        ok, peak = loopback_verify(spk_hw, mic_hw)
        if ok:
            print(f"  Verified (peak: {peak:.0f})")
        else:
            print("  Verification failed. Try selecting a different pair.")
            sys.exit(1)

    spk_rates = test_rates(spk_hw, False)
    mic_rates = test_rates(mic_hw, True)

    write_config(spk_hw, mic_hw, spk_rates or [22050, 44100],
                 mic_rates or [16000], args.config)
    print(f"\nConfig written: {args.config}")
    print(f"  Speaker: {spk_hw}  (rates: {spk_rates or [22050, 44100]})")
    print(f"  Mic:     {mic_hw}  (rates: {mic_rates or [16000]})")
    print(f"\nRun: glados-spoke --config {args.config}")


if __name__ == "__main__":
    main()
