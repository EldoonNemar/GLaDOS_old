#!/usr/bin/env python3
"""Universal GLaDOS Spoke audio device auto-detection."""

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


def is_usb_device(card_name: str) -> bool:
    lower = card_name.lower()
    return ("usb" in lower or "pdp" in lower or "jabra" in lower or
            "plantronics" in lower or "logitech" in lower or
            "blue" in lower or "steelseries" in lower)


def test_capture_device(hw: str, duration: int = 3) -> tuple[bool, float, str]:
    tmp = f"/tmp/glados-capture-{hw.replace(',', '-')}.wav"
    try:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        p = subprocess.Popen(
            ["arecord", "-D", hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", str(duration), tmp],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate(timeout=duration + 3)

        stderr_str = stderr.decode(errors="replace") if stderr else ""
        if "busy" in stderr_str or "No such device" in stderr_str:
            return False, 0, "busy/missing"
        if "Invalid argument" in stderr_str or "Access nonexistent" in stderr_str:
            return False, 0, "invalid"

        if not os.path.exists(tmp):
            return False, 0, "no file"
        if os.path.getsize(tmp) <= 100:
            return False, 0, f"small({os.path.getsize(tmp)}b)"

        with open(tmp, "rb") as f:
            f.read(44)
            raw = f.read()
        if len(raw) < 8:
            return False, 0, "no data"

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        peak = float(np.max(np.abs(samples)))
        if peak < 50:
            return False, peak, f"quiet({peak:.0f})"
        return True, peak, ""

    except subprocess.TimeoutExpired:
        return False, 0, "timeout"
    except Exception as e:
        return False, 0, str(e)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def find_working_mic(card_count: int) -> dict | None:
    print("\n=== Phase 1: Finding Microphone ===")
    print("Recording 3s ambient from each device...\n")

    usb_mics = []
    other_mics = []

    for card in range(card_count):
        card_name = get_card_name(card)
        for dev in range(MAX_DEVICES_PER_CARD):
            hw = f"hw:{card},{dev}"
            success, peak, err = test_capture_device(hw)

            if success:
                is_usb = is_usb_device(card_name)
                if is_usb:
                    usb_mics.append({
                        "hw": hw, "name": card_name, "peak": peak,
                        "card": card, "is_usb": True,
                    })
                    print(f"  + {hw} ({card_name}) [USB] peak={peak:.0f}")
                else:
                    other_mics.append({
                        "hw": hw, "name": card_name, "peak": peak,
                        "card": card, "is_usb": False,
                    })
                    print(f"  . {hw} ({card_name}) peak={peak:.0f}")
            else:
                print(f"  - {hw}: {err}")

    if usb_mics:
        best = max(usb_mics, key=lambda m: m["peak"])
        print(f"\n  Selected USB mic: {best['hw']} (card {best['card']})")
        return best
    if other_mics:
        best = max(other_mics, key=lambda m: m["peak"])
        print(f"\n  Selected: {best['hw']} (card {best['card']})")
        return best
    print("\n  No working mic found.")
    return None


def find_working_speaker(mic_hw: str, card_count: int) -> dict | None:
    """Play tone through each device, record with mic, check detection.

    This is the simplest, most universal approach:
    1. Play 1kHz tone through device
    2. Record with mic
    3. If mic hears it, that device COULD be the speaker
    4. Pick the one with highest peak
    """
    print("\n=== Phase 2: Finding Speaker ===")
    print("Playing tone through each device, recording with mic...\n")

    detected = []

    for card in range(card_count):
        card_name = get_card_name(card)
        for dev in range(MAX_DEVICES_PER_CARD):
            hw = f"hw:{card},{dev}"

            output = f"/tmp/glados-spk-{hw.replace(',', '-')}.wav"
            try:
                try:
                    os.unlink(output)
                except OSError:
                    pass

                spk = subprocess.Popen(
                    ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
                     "-c", "2", "-D", hw],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)

                rec = subprocess.Popen(
                    ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
                     "-c", "1", "-d", "5", output],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = rec.communicate(timeout=10)

                spk.terminate()
                try:
                    spk.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    spk.kill()
                kill_speaker_test(hw)

                if os.path.exists(output) and os.path.getsize(output) > 200:
                    with open(output, "rb") as f:
                        f.read(44)
                        raw = f.read()
                    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                    peak = float(np.max(np.abs(samples)))

                    if peak > 100:
                        detected.append({
                            "hw": hw, "name": card_name, "card": card,
                            "peak": peak,
                        })
                        print(f"  + {hw} ({card_name}) heard (peak: {peak:.0f})")
                    else:
                        print(f"  - {hw} ({card_name}): too quiet (peak: {peak:.0f})")
                else:
                    print(f"  - {hw} ({card_name}): no recording")

            except Exception as e:
                print(f"  - {hw}: error: {e}")
            finally:
                try:
                    os.unlink(output)
                except OSError:
                    pass

    if detected:
        best = max(detected, key=lambda d: d["peak"])
        print(f"\n  Selected speaker: {best['hw']} (peak: {best['peak']:.0f})")
        if len(detected) > 1:
            others = [d['hw'] for d in detected if d['hw'] != best['hw']]
            print(f"  Also detected: {others}")
        return best

    print("\n  No speaker detected. The mic didn't hear any device.")
    return None


def loopback_verify(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    output = "/tmp/glados-verify.wav"
    try:
        try:
            os.unlink(output)
        except OSError:
            pass
        spk = subprocess.Popen(
            ["speaker-test", "-t", "sine", "-f", "1000", "-l", "1",
             "-c", "2", "-D", speaker_hw],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        rec = subprocess.Popen(
            ["arecord", "-D", mic_hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", "5", output],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = rec.communicate(timeout=10)
        spk.terminate()
        try:
            spk.wait(timeout=3)
        except subprocess.TimeoutExpired:
            spk.kill()

        if not os.path.exists(output) or os.path.getsize(output) <= 200:
            return False, 0.0

        with open(output, "rb") as f:
            f.read(44)
            raw = f.read()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        peak = float(np.max(np.abs(samples)))
        return peak >= 100, peak

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
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                p = subprocess.Popen(
                    ["arecord", "-D", hw, "-r", str(rate), "-f", "S16_LE",
                     "-c", "1", "-d", "1", tmp],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = p.communicate(timeout=5)
                if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 44:
                    rates.append(rate)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            else:
                p = subprocess.Popen(
                    ["speaker-test", "-t", "silence", "-r", str(rate),
                     "-l", "0", "-c", "2", "-D", hw],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)
                stdout, stderr = p.communicate(timeout=3)
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

    mic_info = find_working_mic(card_count)
    if not mic_info:
        print("\nERROR: No working microphone found.")
        sys.exit(1)

    mic_hw = mic_info["hw"]

    spk_info = find_working_speaker(mic_hw, card_count)
    if not spk_info:
        print("\nERROR: No speaker detected by the microphone.")
        print("Try: glados-spoke setup --manual")
        sys.exit(1)

    spk_hw = spk_info["hw"]

    print("\n=== Phase 3: Verifying Pair ===")
    ok, peak = loopback_verify(spk_hw, mic_hw)
    if ok:
        print(f"  ✓ Verified (peak: {peak:.0f})")
    else:
        print("  ✗ Verification failed.")
        sys.exit(1)

    print("\n=== Testing Sample Rates ===")
    spk_rates = test_rates(spk_hw, False)
    mic_rates = test_rates(mic_hw, True)
    print(f"  Speaker {spk_hw}: {spk_rates or [22050, 44100]}")
    print(f"  Mic     {mic_hw}: {mic_rates or [16000]}")

    write_config(spk_hw, mic_hw, spk_rates or [22050, 44100],
                 mic_rates or [16000], args.config)
    print(f"\nConfig written: {args.config}")
    print(f"  Speaker: {spk_hw}  ({spk_info['name']})")
    print(f"  Mic:     {mic_hw}  ({mic_info['name']})")
    print(f"\nRun: glados-spoke --config {args.config}")


if __name__ == "__main__":
    main()
