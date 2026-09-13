"""AI Drive-Thru voice pipeline entry point.

The SAME code runs two ways:

  * Camera present (Raspberry Pi): fully automatic, presence-gated — YOLOv8n
    detects a person, recording starts by itself, and a photo is captured.
  * No camera (a PC): keypress-driven fallback — press Enter to take an order,
    no photo is captured. Useful for development and as a safety net.

Flow:
1. wait_for_confirmed_person()  — person seen ~1s (or a keypress in manual mode)
2. record_while_present()       — record until they leave / cap / fixed window
3. stt.transcribe()             — Whisper (faster-whisper, auto-detects language)
4. deepseek_client.parse_order()— DeepSeek -> structured order (or clarification)
5. on clarification: speak the question and listen again
6. capture_photo()              — clean still (camera mode only)
7. POST /order + /upload_photo  — send to the backend
8. speak a confirmation (in the customer's language), wait for them to leave, reset

Run from the project root:   python voice_pipeline/main.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import requests

from camera import ManualDetector, PresenceDetector
from deepseek_client import load_menu, parse_order
import mic_capture
import stt
import tts_speaker

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
TEMP_DIR = os.environ.get("TEMP_DIR", "/tmp/ai_drive_thru")
SAFETY_CAP_SECONDS = float(os.environ.get("SAFETY_CAP_SECONDS", "15"))
MAX_CLARIFICATIONS = int(os.environ.get("MAX_CLARIFICATIONS", "2"))


def _make_detector():
    """Pick the presence detector: camera if available, keypress otherwise.

    On a headless box (a Pi sealed in an enclosure, running under systemd) we do
    NOT fall back to keypress mode — there is no keyboard, so it's better to
    raise and let the service manager restart us (retrying the camera) than to
    hang on an input() prompt.
    """
    try:
        detector = PresenceDetector()
        print("[pipeline] camera ready — presence-gated mode")
        return detector
    except Exception as exc:
        if not sys.stdin.isatty():
            raise
        print(f"[pipeline] no camera ({exc}) — manual (keypress) mode")
        return ManualDetector()


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
        time.sleep(2.0)  # no camera: brief pause before the next keypress
        return
    start = time.time()
    while time.time() - start < timeout:
        if not detector.is_person_still_present():
            return
        time.sleep(0.5)


def _confirmation(total: float, lang: str | None) -> tuple[str, str | None]:
    """Return (message, speak_language) for the order confirmation."""
    if lang == "ta":
        return (
            f"உங்கள் ஆர்டர் உறுதி செய்யப்பட்டது. மொத்தம் {total:.2f} ரூபாய். "
            f"தயவுசெய்து முன்னால் நகர்ந்து செல்லுங்கள்.",
            "ta",
        )
    return f"Order confirmed. Your total is {total:.2f} rupees. Please pull forward.", None


def main() -> None:
    os.makedirs(TEMP_DIR, exist_ok=True)

    detector = _make_detector()
    # Manual mode records a short fixed window; camera mode records until the
    # person leaves (with SAFETY_CAP_SECONDS as the hard ceiling).
    record_seconds = getattr(detector, "record_seconds", None) or SAFETY_CAP_SECONDS
    print("[pipeline] watching for customers")

    try:
        while True:
            # 1. wait for a person (camera) or a keypress (manual)
            detector.wait_for_confirmed_person()
            print("[pipeline] customer present — listening")

            # 2. record
            audio = mic_capture.record_while_present(
                detector.is_person_still_present, max_seconds=record_seconds
            )
            if audio is None:
                continue

            # 3. transcribe (auto-detects the language)
            transcript, lang = stt.transcribe_with_language(audio)
            print(f"[pipeline] heard ({lang}): {transcript!r}")

            # 4-5. parse; loop on clarification (bounded — can't spin forever)
            menu = load_menu()
            result = None
            for _ in range(MAX_CLARIFICATIONS + 1):
                try:
                    result = parse_order(transcript, menu)
                except Exception as exc:
                    print(f"[pipeline] parse failed: {exc}")
                    tts_speaker.speak(
                        "Sorry, I had trouble with your order. Please pull forward.", lang
                    )
                    result = None
                    break

                if result.get("status") != "clarification":
                    break

                tts_speaker.speak(result.get("question", "Could you repeat that?"), lang)
                audio = mic_capture.record_while_present(
                    detector.is_person_still_present, max_seconds=record_seconds
                )
                if audio is None:
                    result = None  # customer left mid-clarification
                    break
                transcript, lang = stt.transcribe_with_language(audio)
                if not transcript.strip():
                    print("[pipeline] silence after clarification — customer left")
                    result = None
                    break
                print(f"[pipeline] heard ({lang}) follow-up: {transcript!r}")
            else:
                # exhausted the clarification budget
                tts_speaker.speak(
                    "Sorry, I'm still not sure. Please pull forward to the window.", lang
                )
                result = None

            if result is None or result.get("status") != "ok":
                _wait_until_absent(detector)
                continue

            # 6. capture a clean photo now (order parsed, not mid-sentence)
            order_id = short_uuid()
            local_photo = os.path.join(TEMP_DIR, f"{order_id}.jpg")
            if not detector.capture_photo(local_photo):
                local_photo = None

            # 7. send the order to the backend
            try:
                post_order(order_id, result["items"], result["total_price"])
                if local_photo:
                    try:
                        upload_photo(order_id, local_photo)
                    except Exception as exc:
                        print(f"[pipeline] photo upload failed: {exc}")
            except Exception as exc:
                print(f"[pipeline] order POST failed (is the backend running?): {exc}")

            # 8. confirm aloud in the customer's language
            message, speak_lang = _confirmation(result["total_price"], lang)
            tts_speaker.speak(message, speak_lang)

            # 9. wait for them to leave, then reset for the next customer
            _wait_until_absent(detector)
            print("[pipeline] customer gone — watching again")
    except KeyboardInterrupt:
        print("\n[pipeline] shutting down")
    finally:
        detector.release()


if __name__ == "__main__":
    main()
