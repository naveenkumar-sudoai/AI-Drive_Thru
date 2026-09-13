"""AI Drive-Thru voice pipeline entry point.

Runs in one of four modes (PIPELINE_MODE env, default "auto"):

  * "camera"   — presence-gated via picamera2/cv2 + YOLOv8n (Raspberry Pi).
  * "wakeword" — mic stays on, activates when the wake word is heard (no camera).
  * "manual"   — press Enter to start each order (quick PC test).
  * "auto"     — try camera; if there's none (and a tty), fall back to wake word.

Flow (after an order is spoken and parsed):
  - ask the customer "your order is … — is that correct?" (spoken + OLED)
  - only submit the order to the backend once they say yes; handle changes
  - show the order and confirmation on a 0.96" OLED if one is connected

All spoken output is English (native-language TTS caused issues and will be
revisited later).

Run from the project root:   python voice_pipeline/main.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import requests

from camera import ManualDetector, PresenceDetector
from deepseek_client import confirm_order, load_menu, parse_order, summarize_order
from display import Display
from wake_word import WakeWordDetector
import mic_capture
import stt
import tts_speaker

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
TEMP_DIR = os.environ.get("TEMP_DIR", "/tmp/ai_drive_thru")
SAFETY_CAP_SECONDS = float(os.environ.get("SAFETY_CAP_SECONDS", "15"))
MAX_CLARIFICATIONS = int(os.environ.get("MAX_CLARIFICATIONS", "2"))
MAX_CONFIRMATIONS = int(os.environ.get("MAX_CONFIRMATIONS", "2"))
CONFIRM_SECONDS = float(os.environ.get("CONFIRM_SECONDS", "4"))


def _make_detector():
    """Pick the detector based on PIPELINE_MODE (camera / wakeword / manual / auto)."""
    mode = os.environ.get("PIPELINE_MODE", "auto").strip().lower()

    if mode == "camera":
        return PresenceDetector()
    if mode == "wakeword":
        return WakeWordDetector()
    if mode == "manual":
        return ManualDetector()

    # auto: camera first, then wake-word fallback (interactive terminals only).
    try:
        detector = PresenceDetector()
        print("[pipeline] camera ready — presence-gated mode")
        return detector
    except Exception as exc:
        if not sys.stdin.isatty():
            raise  # headless (systemd): let the service manager restart us
        print(f"[pipeline] no camera ({exc}) — wake-word mode")
        return WakeWordDetector()


def short_uuid() -> str:
    return uuid.uuid4().hex[:8]


def post_order(order_id: str, items: list, total_price: float) -> dict:
    payload = {
        "order_id": order_id,
        "items": items,
        "total_price": total_price,
        "photo_path": None,
        "timestamp": time.time(),
        "status": "pending",
    }
    resp = requests.post(f"{BACKEND_URL}/order", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def upload_photo(order_id: str, local_path: str) -> dict:
    with open(local_path, "rb") as f:
        resp = requests.post(
            f"{BACKEND_URL}/upload_photo/{order_id}",
            files={"file": (f"{order_id}.jpg", f, "image/jpeg")},
            timeout=10,
        )
    resp.raise_for_status()
    return resp.json()


def _wait_until_absent(detector, timeout: float = 20.0) -> None:
    """Wait for the customer to leave before watching for the next one."""
    if getattr(detector, "manual", False):
        time.sleep(2.0)  # no camera: brief pause before the next trigger
        return
    start = time.time()
    while time.time() - start < timeout:
        if not detector.is_person_still_present():
            return
        time.sleep(0.5)


def _confirmation(total: float) -> str:
    return f"Order confirmed. Your total is {total:.2f} rupees. Please pull forward."


def main() -> None:
    os.makedirs(TEMP_DIR, exist_ok=True)

    detector = _make_detector()
    record_seconds = getattr(detector, "record_seconds", None) or SAFETY_CAP_SECONDS
    display = Display()
    display.show_status("Starting…")
    print("[pipeline] watching for customers")

    try:
        while True:
            # 1. wait for a person / wake word / keypress
            detector.wait_for_confirmed_person()
            display.show_status("Listening…")
            print("[pipeline] customer present — listening")

            # 2. record the order
            audio = mic_capture.record_while_present(
                detector.is_person_still_present, max_seconds=record_seconds
            )
            if audio is None:
                continue

            # 3. transcribe
            transcript = stt.transcribe(audio)
            print(f"[pipeline] heard: {transcript!r}")

            # 4-5. parse; loop on clarification (bounded — can't spin forever)
            menu = load_menu()
            result = None
            for _ in range(MAX_CLARIFICATIONS + 1):
                try:
                    result = parse_order(transcript, menu)
                except Exception as exc:
                    print(f"[pipeline] parse failed: {exc}")
                    tts_speaker.speak(
                        "Sorry, I had trouble with your order. Please pull forward."
                    )
                    result = None
                    break

                if result.get("status") != "clarification":
                    break

                tts_speaker.speak(result.get("question", "Could you repeat that?"))
                audio = mic_capture.record_while_present(
                    detector.is_person_still_present, max_seconds=record_seconds
                )
                if audio is None:
                    result = None  # customer left mid-clarification
                    break
                transcript = stt.transcribe(audio)
                if not transcript.strip():
                    print("[pipeline] silence after clarification — customer left")
                    result = None
                    break
                print(f"[pipeline] heard follow-up: {transcript!r}")
            else:
                tts_speaker.speak(
                    "Sorry, I'm still not sure. Please pull forward to the window."
                )
                result = None

            if result is None or result.get("status") != "ok":
                display.show_status("Waiting…")
                _wait_until_absent(detector)
                continue

            # 6. confirm the order with the customer before submitting
            summary = summarize_order(result["items"], result["total_price"])
            confirmed = False
            for _ in range(MAX_CONFIRMATIONS):
                display.show_order(result["items"], result["total_price"])
                tts_speaker.speak(f"Your order is {summary}. Is that correct?")
                audio = mic_capture.record_while_present(
                    detector.is_person_still_present, max_seconds=CONFIRM_SECONDS
                )
                if audio is None:
                    break
                answer = stt.transcribe(audio)
                print(f"[pipeline] confirmation answer: {answer!r}")
                if not answer.strip():
                    break

                try:
                    decision = confirm_order(answer, summary)
                except Exception as exc:
                    print(f"[pipeline] confirm failed: {exc}")
                    decision = "unclear"

                if decision == "yes":
                    confirmed = True
                    break
                if decision in ("no", "change"):
                    tts_speaker.speak("Okay, please tell me your full order again.")
                    audio = mic_capture.record_while_present(
                        detector.is_person_still_present, max_seconds=record_seconds
                    )
                    if audio is None:
                        break
                    transcript = stt.transcribe(audio)
                    result = parse_order(transcript, menu)
                    if result.get("status") != "ok":
                        result = None
                        break
                    summary = summarize_order(result["items"], result["total_price"])
                    continue
                # unclear — ask again
                tts_speaker.speak("Sorry, I didn't catch that. Is your order correct?")

            if not confirmed:
                display.show_status("Waiting…")
                _wait_until_absent(detector)
                continue

            # 7. capture a clean photo now (camera mode only)
            order_id = short_uuid()
            local_photo = os.path.join(TEMP_DIR, f"{order_id}.jpg")
            if not detector.capture_photo(local_photo):
                local_photo = None

            # 8. send the order to the backend
            try:
                post_order(order_id, result["items"], result["total_price"])
                if local_photo:
                    try:
                        upload_photo(order_id, local_photo)
                    except Exception as exc:
                        print(f"[pipeline] photo upload failed: {exc}")
            except Exception as exc:
                print(f"[pipeline] order POST failed (is the backend running?): {exc}")

            # 9. confirm aloud
            display.show_order(result["items"], result["total_price"], "Confirmed ✓")
            tts_speaker.speak(_confirmation(result["total_price"]))

            # 10. wait for them to leave, then reset for the next customer
            _wait_until_absent(detector)
            display.show_status("Waiting…")
            print("[pipeline] customer gone — watching again")
    except KeyboardInterrupt:
        print("\n[pipeline] shutting down")
    finally:
        display.clear()
        detector.release()


if __name__ == "__main__":
    main()
