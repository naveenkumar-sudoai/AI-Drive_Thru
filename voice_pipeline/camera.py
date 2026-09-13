"""Presence detection + photo capture for the AI Drive-Thru.

This is the Raspberry Pi camera module. It swaps the PC webcam backend
(cv2.VideoCapture) for `picamera2` (libcamera) while keeping the exact same
public interface, so nothing else in the pipeline needs to change:

    detector = PresenceDetector()
    detector.wait_for_confirmed_person()   # blocks until a person is seen ~1s
    detector.is_person_still_present()     # single-frame poll -> True/False
    detector.capture_photo("/tmp/x.jpg")   # full-res still at end of order
    detector.release()

Person detection runs YOLOv8n ("nano") on a 320x240 frame sampled every ~0.5s
(not every frame) to keep CPU load low on a Pi 5 with no GPU accelerator.

IMPORTANT / FLAG: if YOLOv8n (via torch) proves too slow on the Pi 5 CPU at
320x240 @ 0.5 Hz in testing, do NOT silently change the approach. Instead, note
it and either (a) export the model to ONNX/NCNN for faster CPU inference, or
(b) drop detection resolution further (e.g. 256x192 / 224x224). See README.md.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np


class PresenceDetector:
    """Detect people with YOLOv8n and capture stills.

    Uses picamera2 when available (Raspberry Pi), otherwise falls back to
    OpenCV VideoCapture(0) so the same code runs on a PC during development.
    """

    PERSON_CLASS_ID = 0  # COCO class index for "person"

    def __init__(
        self,
        model_path: str = "yolov8n.pt",
        detect_size: tuple = (320, 240),
        photo_size: tuple = (1280, 720),
        confirm_seconds: float = 1.0,
        sample_interval: float = 0.5,
        confidence: float = 0.4,
    ) -> None:
        self.detect_size = detect_size
        self.photo_size = photo_size
        self.confirm_seconds = confirm_seconds
        self.sample_interval = sample_interval
        self.confidence = confidence

        # YOLO model (ultralytics). First run downloads yolov8n.pt (~6 MB).
        from ultralytics import YOLO

        self.model = YOLO(model_path)

        self.backend = "cv2"
        self.picam2 = None
        self.cap = None

        # Try picamera2 (Pi) first, fall back to OpenCV (PC).
        try:
            from picamera2 import Picamera2  # noqa: F401
        except ImportError:
            picamera2_available = False
        else:
            picamera2_available = True

        if picamera2_available:
            try:
                self._init_picamera2()
                self.backend = "picamera2"
                return
            except Exception as exc:  # camera not connected / permission issue
                print(f"[camera] picamera2 init failed ({exc}); falling back to cv2")

        self._init_cv2()

    # -- backend setup ---------------------------------------------------

    def _init_picamera2(self) -> None:
        from picamera2 import Picamera2

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": self.photo_size, "format": "RGB888"},
            controls={"FrameRate": 10},
        )
        self.picam2.configure(config)
        self.picam2.start()

    def _init_cv2(self) -> None:
        import cv2

        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            raise RuntimeError("Could not open camera (index 0)")

    # -- frame grabbing --------------------------------------------------

    def _grab(self) -> Optional[np.ndarray]:
        """Grab one frame as a BGR numpy array (OpenCV convention), or None."""
        import cv2

        if self.backend == "picamera2":
            arr = self.picam2.capture_array()
            if arr is None:
                return None
            # picamera2 with RGB888 returns an (H, W, 3) RGB array.
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        else:
            ok, frame = self.cap.read()
            return frame if ok else None

    def _person_present(self) -> bool:
        frame = self._grab()
        if frame is None:
            return False
        import cv2

        small = cv2.resize(frame, self.detect_size)
        results = self.model(small, verbose=False, conf=self.confidence)
        result = results[0]
        if result.boxes is None:
            return False
        for cls in result.boxes.cls.tolist():
            if int(cls) == self.PERSON_CLASS_ID:
                return True
        return False

    # -- public interface ------------------------------------------------

    def wait_for_confirmed_person(self) -> None:
        """Block until a person has been continuously visible for ~1 second."""
        present_since: Optional[float] = None
        while True:
            if self._person_present():
                now = time.monotonic()
                if present_since is None:
                    present_since = now
                elif now - present_since >= self.confirm_seconds:
                    return
            else:
                present_since = None
            time.sleep(self.sample_interval)

    def is_person_still_present(self) -> bool:
        """Single-frame poll used by mic_capture to decide when to stop."""
        return self._person_present()

    def capture_photo(self, save_path: str) -> bool:
        """Capture a full-resolution still and save it as a JPEG."""
        import cv2

        frame = self._grab()
        if frame is None:
            return False
        directory = os.path.dirname(save_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        return cv2.imwrite(save_path, frame)

    def release(self) -> None:
        if self.backend == "picamera2" and self.picam2 is not None:
            try:
                self.picam2.stop()
            finally:
                self.picam2.close()
        elif self.cap is not None:
            self.cap.release()


class ManualDetector:
    """No-camera stand-in with the same interface as PresenceDetector.

    Lets the SAME pipeline run on a camera-less PC (or as an interactive-dev
    fallback): each order is started with a keypress and records for a fixed
    duration instead of presence-gating. ``capture_photo()`` is a no-op, so
    orders carry no photo and the dashboard shows its placeholder avatar.
    """

    manual = True  # main.py uses this to skip the "wait until absent" camera poll

    def __init__(self, record_seconds: float = 8.0) -> None:
        self.record_seconds = record_seconds

    def wait_for_confirmed_person(self) -> None:
        input("\n[pipeline] Press Enter when a customer is at the speaker (Ctrl+C to quit)...")

    def is_person_still_present(self) -> bool:
        return True  # record for record_seconds / the safety cap

    def capture_photo(self, save_path: str) -> bool:
        return False

    def release(self) -> None:
        pass
