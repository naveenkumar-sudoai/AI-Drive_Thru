"""Text-to-speech for the AI Drive-Thru.

Primary: pyttsx3 (drives espeak/espeak-ng on Linux, SAPI5 on Windows, NSSpeech
on macOS). Falls back to invoking espeak/espeak-ng directly if pyttsx3 is missing
or fails. A non-English ``lang`` (e.g. Tamil "ta") is spoken via espeak-ng's
``-v`` flag, which supports many languages.

On Raspberry Pi OS install:  sudo apt install -y espeak-ng espeak-ng-espeak
"""

from __future__ import annotations

import shutil
import subprocess

_engine = None
_engine_failed = False


def _get_engine():
    global _engine
    if _engine is None:
        import pyttsx3

        _engine = pyttsx3.init()
        _engine.setProperty("rate", 170)
    return _engine


def _speak_cli(text: str, lang: str | None = None) -> bool:
    """Speak via the espeak/espeak-ng binary directly. Returns True on success."""
    exe = shutil.which("espeak") or shutil.which("espeak-ng")
    if not exe:
        return False
    cmd = [exe]
    if lang:
        cmd += ["-v", lang]
    cmd.append(text)
    try:
        subprocess.run(cmd, check=False, capture_output=True)
        return True
    except Exception:
        return False


def speak(text: str, lang: str | None = None) -> None:
    """Speak ``text`` synchronously. ``lang`` is an ISO 639-1 code (e.g. "ta")."""
    if not text:
        return

    # A non-English language is spoken through espeak-ng's native voice support.
    if lang and lang != "en":
        if _speak_cli(text, lang):
            return

    global _engine_failed
    if not _engine_failed:
        try:
            engine = _get_engine()
            engine.say(text)
            engine.runAndWait()
            return
        except Exception:
            _engine_failed = True  # don't retry pyttsx3; use the CLI fallback

    if not _speak_cli(text):
        print(f"[tts] (unable to speak): {text}")
