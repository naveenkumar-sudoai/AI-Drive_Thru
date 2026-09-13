"""Microphone capture for the AI Drive-Thru.

Records audio only while the customer is present (no push-to-talk). Recording
stops when the person leaves the frame or the safety cap is reached.

Uses `sounddevice` (PortAudio), which works on both PC and Raspberry Pi.
"""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import sounddevice as sd


def record_while_present(
    present_check: Callable[[], bool],
    sample_rate: int = 16000,
    max_seconds: float = 15.0,
    chunk_seconds: float = 0.25,
) -> Optional[np.ndarray]:
    """Record mono audio while ``present_check()`` returns True.

    Arguments:
        present_check: callable returning whether the customer is still present.
        sample_rate:   audio sample rate in Hz (Whisper default is 16 kHz).
        max_seconds:   hard cap on recording length.
        chunk_seconds: how often to poll ``present_check`` (and how audio is
                       chunked).

    Returns a float32 mono numpy array, or None if nothing was recorded.
    """
    if not present_check():
        return None

    chunks: list[np.ndarray] = []
    chunk_frames = max(1, int(sample_rate * chunk_seconds))
    elapsed = 0.0

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=chunk_frames,
    ) as stream:
        while elapsed < max_seconds:
            audio, _ = stream.read(chunk_frames)
            chunks.append(audio.copy())
            elapsed += chunk_seconds
            if not present_check():
                break

    if not chunks:
        return None
    return np.concatenate(chunks).flatten()
