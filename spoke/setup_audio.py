#!/usr/bin/env python3
"""Universal GLaDOS Spoke audio device auto-detection.

Phase 1: Find the mic that actually picks up sound
Phase 2: Use that mic to find which speaker it can hear  
Phase 3: Loopback verify the pair

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


def test_capture_device(hw: str, duration: int = 3) -> tuple[bool, float, str]:
    """Test if a capture device works.

    Records for duration seconds. Requires:
    - File created with size > 100 bytes (WAV header + some data)
    - Peak > 50 (real audio, not electrical noise)
    - No ALSA errors

    Returns (success, peak, error_msg).
    """
    tmp = f"/tmp/glados-capture-{hw.replace(',', '-')}.wav"
    try:
        try:
            os.unlink(tmp)
        except OSError:
            pass

        p = subprocess.Popen(
            ["arecord", "-D", hw, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", str(duration), tmp],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        stdout, stderr = p.communicate(timeout=duration + 3)
        rc = p.returncode

        stderr_str = stderr.decode(errors="replace") if stderr else ""
        if "Device or resource busy" in stderr_str or "No such device" in stderr_str:
            return False, 0, f"ALSA error"
        if "Access nonexistent device" in stderr_str or "Invalid argument" in stderr_str:
            return False, 0, "Invalid device"

        if not os.path.exists(tmp):
            return False, 0, "File not created"

        file_size = os.path.getsize(tmp)
        if file_size <= 100:
            return False, 0, f"File too small ({file_size}b)"

        with open(tmp, "rb") as f:
            f.read(44)
            raw = f.read()

        if len(raw) < 8:
            return False, 0, "No audio data"

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        peak = float(np.max(np.abs(samples)))

        # USB/Conference mics typically pick up 100-5000+ when room noise present
        # Electrical noise/hdmi-in typically < 50
        if peak < 50:
            return False, peak, f"Too quiet (peak: {peak:.0f})"

        return True, peak, ""

    except subprocess.TimeoutExpired:
        return False, 0, "Timeout"
    except Exception as e:
        return False, 0, str(e)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def is_usb_device(card_name: str) -> bool:
    """Check if device is USB-based (conference mic, USB headset, etc.)."""
    lower = card_name.lower()
    return ("usb" in lower or "pdp" in lower or "jabra" in lower or
            "plantronics" in lower or "logitech" in lower or
            "blue" in lower or "steelseries" in lower)


def find_working_mic(card_count: int) -> dict | None:
    """Find the mic that actually picks up sound."""
    print("\n=== Phase 1: Finding Microphone ===")
    print("Recording 3 seconds of ambient sound from each device...\n")

    usb_mics = []
    other_mics = []

    for card in range(card_count):
        card_name = get_card_name(card)
        for dev in range(MAX_DEVICES_PER_CARD):
            hw = f"hw:{card},{dev}"

            success, peak, err = test_capture_device(hw)

            if success:
                is_usb = is_usb_device(card_name)
                usb_mark = " [USB/Conference]" if is_usb else ""
                status = "✓"

                entry = {
                    "hw": hw,
                    "name": f"{card_name} (hw:{card},{dev}) [in]",
                    "peak": peak,
                    "is_usb": is_usb,
                    "usb_mark": usb_mark,
                }

                if is_usb:
                    usb_mics.append(entry)
                    print(f"  {status} {hw} ({card_name}){usb_mark} peak={peak:.0f} [USB PREFERRED]")
                else:
                    other_mics.append(entry)
                    print(f"  - {hw} ({card_name}){usb_mark} peak={peak:.0f}")
            else:
                print(f"  . {hw}: {err}")

    # USB mics are ALWAYS preferred over other mics
    if usb_mics:
        # Among USB mics, pick the one with highest peak
        best = max(usb_mics, key=lambda m: m["peak"])
        print(f"\n  Selected USB mic: {best['hw']} (peak: {best['peak']:.0f})")
        return best

    if other_mics:
        # Among other mics, pick the one with highest peak
        best = max(other_mics, key=lambda m: m["peak"])
        print(f"\n  Selected: {best['hw']} (peak: {best['peak']:.0f})")
        return best

    print("\n  No working mic found.")
    return None


def find_working_speaker(mic_hw: str, card_count: int) -> dict | None:
    """Find which speaker the chosen mic can hear."""
    print("\n=== Phase 2: Finding Speaker ===")
    print("Playing 1kHz tone through each device, recording with the mic...\n")

    best_spk = None
    best_peak = 0
    found_any = False

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
                     "-c", "1", "-d", "3", output],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = rec.communicate(timeout=6)

                spk.terminate()
                try:
                    spk.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    spk.kill()
                kill_speaker_test(hw)

                if os.path.exists(output) and os.path.getsize(output) > 100:
                    with open(output, "rb") as f:
                        f.read(44)
                        raw = f.read()
                    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                    peak = float(np.max(np.abs(samples)))

                    if peak > 100:  # Higher threshold for speaker detection
                        found_any = True
                        print(f"  ✓ {hw} ({card_name}) heard (peak: {peak:.0f})")
                        if peak > best_peak:
                            best_peak = peak
                            best_spk = {
                                "hw": hw,
                                "name": f"{card_name} (hw:{card},{dev}) [out]",
                            }
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

    if found_any and best_spk:
        print(f"\n  Selected speaker: {best_spk['hw']} (peak: {best_peak:.0f})")
        return best_spk

    print("\n  No speaker detected by mic.")
    return None


def loopback_verify(speaker_hw: str, mic_hw: str) -> tuple[bool, float]:
    """Final verification."""
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
             "-c", "1", "-d", "4", output],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = rec.communicate(timeout=8)
        spk.terminate()
        try:
            spk.wait(timeout=3)
        except subprocess.TimeoutExpired:
            spk.kill()

        if not os.path.exists(output) or os.path.getsize(output) <= 100:
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
