"""Speech-to-text for the AI Drive-Thru.

Uses faster-whisper (Whisper models on CTranslate2) — far faster than PyTorch
Whisper on a Raspberry Pi CPU. The model is multilingual, so it auto-detects the
customer's language (English, Tamil, Hindi, ...). Set WHISPER_LANGUAGE (e.g.
"ta" or "en") to force a language and skip detection. Model size is set with
WHISPER_MODEL (default "base"; "tiny" is faster on a Pi 5).

The first call downloads the model from the Hugging Face hub.
"""

from __future__ import annotations

import os

import numpy as np

_model = None
_model_size = None


def _get_model():
    global _model, _model_size
    size = os.environ.get("WHISPER_MODEL", "base")
    if _model is None or size != _model_size:
        from faster_whisper import WhisperModel

        # int8 quantization keeps memory and CPU usage low on the Pi.
        _model = WhisperModel(size, device="cpu", compute_type="int8")
        _model_size = size
    return _model


def transcribe_with_language(audio: np.ndarray, sample_rate: int = 16000):
    """Transcribe and return ``(text, detected_language)``.

    ``detected_language`` is an ISO 639-1 code (e.g. "ta", "en") or None when
    nothing is recognized.
    """
    if audio is None or len(audio) == 0:
        return "", None
    language = os.environ.get("WHISPER_LANGUAGE") or None
    model = _get_model()
    segments, info = model.transcribe(audio, language=language, beam_size=5)
    text = "".join(segment.text for segment in segments).strip()
    return text, getattr(info, "language", None)


def transcribe(audio: np.ndarray, sample_rate: int = 16000) -> str:
    """Transcribe a float32 mono audio array to text (backward-compatible)."""
    text, _ = transcribe_with_language(audio, sample_rate)
    return text
