"""Wake-word detection for the AI Drive-Thru (no-camera mode).

Keeps the microphone on and listens for a configurable wake word (default
"friday"). When it hears the word it returns, so the pipeline can start taking
the order. Reuses faster-whisper ("tiny"), so no extra model is needed.

This is the no-camera fallback. On the Raspberry Pi the camera presence detector
is used instead (lighter on CPU than continuous transcription). Config via env:
WAKE_WORD (default "friday"), WAKE_WORD_MODEL (default "tiny").
"""

from __future__ import annotations

import os
import time

import numpy as np

WAKE_WORD = os.environ.get("WAKE_WORD", "friday").lower().strip()
WAKE_WORD_MODEL = os.environ.get("WAKE_WORD_MODEL", "tiny")
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "16000"))


class WakeWordDetector:
    """Same interface as PresenceDetector / ManualDetector.

    ``wait_for_confirmed_person()`` blocks until the wake word is heard;
    ``is_person_still_present()`` then returns True so the pipeline records for a
    fixed window; ``capture_photo()`` is a no-op (no camera).
    """

    manual = True  # skip the camera-based "wait until absent" poll in main.py

    def __init__(self, wake_word: str | None = None, record_seconds: float = 8.0) -> None:
        self.wake_word = (wake_word or WAKE_WORD).lower()
        self.record_seconds = record_seconds
        self.model = None

    def _load_model(self):
        if self.model is None:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(WAKE_WORD_MODEL, device="cpu", compute_type="int8")
        return self.model

    def wait_for_confirmed_person(self) -> None:
        """Keep the mic on and return once the wake word is heard."""
        import sounddevice as sd

        model = self._load_model()
        window_seconds = 3.0
        window = np.zeros(int(SAMPLE_RATE * window_seconds), dtype=np.float32)
        chunk = int(SAMPLE_RATE * 0.5)  # read 0.5 s at a time
        last_check = 0.0
        print(f"[pipeline] mic on — say '{self.wake_word}' to start an order (Ctrl+C to quit)")

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=chunk
        ) as stream:
            while True:
                audio, _ = stream.read(chunk)
                audio = audio.flatten()
                window = np.roll(window, -len(audio))
                window[-len(audio):] = audio

                # transcribe the rolling window ~every 1.5 s to keep CPU low
                if time.time() - last_check < 1.5:
                    continue
                last_check = time.time()
                segments, _ = model.transcribe(window, language=None, beam_size=1)
                text = "".join(s.text for s in segments).lower()
                if self.wake_word in text:
                    print(f"[pipeline] wake word '{self.wake_word}' heard")
                    return

    def is_person_still_present(self) -> bool:
        return True  # record for record_seconds after the wake word

    def capture_photo(self, save_path: str) -> bool:
        return False

    def release(self) -> None:
        pass
