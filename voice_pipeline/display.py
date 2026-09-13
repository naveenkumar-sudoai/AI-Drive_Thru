"""Optional 0.96" OLED (SSD1306, I2C) display for the AI Drive-Thru.

Shows the current order and asks the customer to confirm on-screen. If the
display isn't connected, or the `luma.oled` library isn't installed, every call
is a safe no-op so the pipeline runs fine without it.

Pi setup:
    sudo raspi-config   -> Interface Options -> I2C -> Enable (then reboot)
    pip install luma.oled
"""

from __future__ import annotations

import os


class Display:
    def __init__(self, port: int = 1, address: int = 0x3C) -> None:
        self.device = None
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1306

            self.device = ssd1306(i2c(port=port, address=address), width=128, height=64)
        except Exception as exc:
            print(f"[display] OLED not available ({exc}) — running headless")

    def show_lines(self, lines) -> None:
        if self.device is None:
            return
        try:
            from luma.core.render import canvas

            with canvas(self.device) as draw:
                y = 0
                for line in lines:
                    draw.text((2, y), str(line)[:20], fill="white")
                    y += 11
        except Exception as exc:
            print(f"[display] render failed: {exc}")

    def show_status(self, text: str) -> None:
        self.show_lines(["AI Drive-Thru", text])

    def show_order(self, items, total: float, question: str = "Correct? yes/no") -> None:
        lines = [f"{it.get('quantity', 1)}x {it.get('name', '?')}" for it in items[:4]]
        if len(items) > 4:
            lines.append(f"+{len(items) - 4} more")
        lines.append(f"Total: Rs {total:.2f}")
        lines.append(question)
        self.show_lines(lines)

    def clear(self) -> None:
        if self.device is None:
            return
        try:
            self.device.clear()
        except Exception:
            pass
