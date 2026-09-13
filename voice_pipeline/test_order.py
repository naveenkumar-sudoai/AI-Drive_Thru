"""Camera-free ordering test for the AI Drive-Thru voice pipeline.

Runs the real mic -> Whisper -> DeepSeek -> TTS chain WITHOUT the presence
detector (useful on a PC that has a mic but no camera). It:

1. records a fixed number of seconds from the microphone
2. transcribes with faster-whisper (auto-detects the language, incl. Tamil)
3. fetches the live menu from the backend (GET /menu)
4. parses the order with DeepSeek
5. speaks the confirmation aloud (in the customer's language)
6. posts the order to the backend so it shows up in the Live Queue

Run from the project root:
    source voice_pipeline/.venv/bin/activate
    export DEEPSEEK_API_KEY="$(cat api_key.txt)"
    python voice_pipeline/test_order.py

Env knobs: RECORD_SECONDS (default 6), BACKEND_URL (default http://localhost:8000).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import requests

# allow running as `python voice_pipeline/test_order.py` from the repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mic_capture
import stt
import tts_speaker
from deepseek_client import load_menu, parse_order

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
RECORD_SECONDS = float(os.environ.get("RECORD_SECONDS", "6"))


def _confirmation(total: float, lang: str | None) -> tuple[str, str | None]:
    if lang == "ta":
        return (
            f"உங்கள் ஆர்டர் உறுதி செய்யப்பட்டது. மொத்தம் {total:.2f} ரூபாய். "
            f"தயவுசெய்து முன்னால் நகர்ந்து செல்லுங்கள்.",
            "ta",
        )
    return f"Order confirmed. Your total is {total:.2f} rupees. Please pull forward.", None


def _list_devices() -> None:
    try:
        import sounddevice as sd

        print("Audio devices:")
        for i, d in enumerate(sd.query_devices()):
            mark = " <-- default" if i == sd.default.device[0] else ""
            if d["max_input_channels"] > 0:
                print(f"  [{i}] {d['name']} (in={d['max_input_channels']}){mark}")
    except Exception as exc:
        print(f"(could not list audio devices: {exc})")


def main() -> None:
    _list_devices()
    print()
    input(f"Press Enter, then speak your order (recording {RECORD_SECONDS:.0f}s)...")
    print("Recording...")
    audio = mic_capture.record_while_present(lambda: True, max_seconds=RECORD_SECONDS)

    if audio is None or len(audio) == 0:
        print("No audio captured — check the microphone.")
        return

    print("Transcribing...")
    transcript, lang = stt.transcribe_with_language(audio)
    print(f"Heard ({lang}):", repr(transcript))

    if not transcript.strip():
        print("Nothing recognized.")
        return

    menu = load_menu()
    result = parse_order(transcript, menu)
    print("Parsed:", json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("status") == "clarification":
        tts_speaker.speak(result.get("question", "Could you repeat that?"), lang)
        return

    total = result["total_price"]
    message, speak_lang = _confirmation(total, lang)
    tts_speaker.speak(message, speak_lang)

    try:
        resp = requests.post(
            f"{BACKEND_URL}/order",
            json={
                "order_id": uuid.uuid4().hex[:8],
                "items": result["items"],
                "total_price": total,
                "photo_path": None,
                "timestamp": time.time(),
                "status": "pending",
            },
            timeout=10,
        )
        resp.raise_for_status()
        print("✅ Order posted to the backend — check the Live Queue in your browser.")
    except Exception as exc:
        print(f"⚠️  Order not posted (is the backend running?): {exc}")


if __name__ == "__main__":
    main()
