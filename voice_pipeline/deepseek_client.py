"""DeepSeek order parsing for the AI Drive-Thru.

The menu is now loaded LIVE from the backend (GET /menu) so the dashboard's
Menu Editor is the single source of truth. If the backend is unreachable (e.g.
running standalone before the backend exists), we fall back to a local
menu.json file.

The DeepSeek call turns a transcript into a structured order:
    {"status": "ok", "items": [...], "total_price": <float>}
or, when something needs clarifying:
    {"status": "clarification", "question": "..."}
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
MENU_JSON_PATH = os.environ.get(
    "MENU_JSON_PATH", os.path.join(os.path.dirname(__file__), "menu.json")
)


# --- menu loading -------------------------------------------------------

def load_menu() -> List[dict]:
    """Fetch the current menu from the backend; fall back to local menu.json.

    Returns a list of items like {"id", "name", "price", "available"} (the
    exact shape GET /menu returns), or the equivalent from menu.json.
    """
    try:
        resp = requests.get(f"{BACKEND_URL}/menu", timeout=3)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return _load_menu_from_file()


def _load_menu_from_file() -> List[dict]:
    """Fallback: read a local menu.json so the pipeline runs standalone."""
    try:
        with open(MENU_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    items = data if isinstance(data, list) else data.get("items", [])
    return [
        {
            "name": it.get("name"),
            "price": float(it.get("price", 0)),
            "available": bool(it.get("available", True)),
        }
        for it in items
        if isinstance(it, dict) and it.get("name")
    ]


def _menu_text(menu: List[dict]) -> str:
    lines = []
    for item in menu:
        if not item.get("available", True):
            continue
        name = item.get("name")
        price = item.get("price", 0)
        lines.append(f"- {name}: ${float(price):.2f}")
    return "\n".join(lines) if lines else "(no menu items available)"


# --- order parsing ------------------------------------------------------

_SYSTEM_PROMPT = """You are the order-taking assistant for a drive-thru restaurant.
Parse the customer's spoken order into structured JSON.

Available menu (these are the ONLY items that can be ordered):
{menu}

Rules:
- Only include items that are on the menu. Match names loosely (e.g. "coke" -> "Coca-Cola").
- Combine duplicate items by summing their quantities.
- "quantity" is a positive integer (default 1).
- Put size/modifier requests in "notes" (e.g. "no pickles", "extra cheese", "large", "diet").
- If the customer says a size or modifier only, fold it into the notes of the relevant item.
- If the order is empty, ambiguous, or mentions an item that is not on the menu,
  ask ONE short, friendly clarification question instead of guessing.

Respond with ONLY a JSON object (no markdown, no prose) in exactly one of these shapes:

A valid order:
{{"status": "ok", "items": [{{"name": "<menu item>", "quantity": <int>, "notes": "<string or empty>"}}], "total_price": <number>}}

A clarification:
{{"status": "clarification", "question": "<short question>"}}"""


def _menu_lookup(menu: List[dict]) -> dict:
    """Map lowercase menu name -> {"name": canonical name, "price": float}.

    Only available items are orderable. Used to canonicalize the model's
    loosely-cased names back to the exact menu name and to price items safely.
    """
    lookup = {}
    for item in menu:
        if not item.get("available", True):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        lookup[name.lower()] = {"name": name, "price": float(item.get("price", 0))}
    return lookup


def parse_order(transcript: str, menu: List[dict]) -> dict:
    """Parse a transcript into a structured order using DeepSeek.

    Returns {"status": "ok", "items": [...], "total_price": <float>}
         or {"status": "clarification", "question": str}.
    """
    if not transcript.strip():
        return {"status": "clarification", "question": "Sorry, I didn't catch that. Could you repeat your order?"}

    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Export it before running the voice pipeline."
        )

    system = _SYSTEM_PROMPT.format(menu=_menu_text(menu))
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": transcript.strip()},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    resp = requests.post(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return {"status": "clarification", "question": "Sorry, I had trouble understanding that. Could you repeat your order?"}

    if result.get("status") == "clarification":
        return result

    # Normalize a valid order and recompute the total from real menu prices so
    # we never trust a hallucinated price.
    lookup = _menu_lookup(menu)
    items = _normalize_items(result.get("items", []), lookup)
    if not items:
        return {"status": "clarification", "question": "Sorry, I couldn't find those items on the menu. What would you like?"}

    total = round(sum(it["price"] * it["quantity"] for it in items), 2)
    # The backend only stores {name, quantity, notes}; drop the internal price.
    clean_items = [
        {"name": it["name"], "quantity": it["quantity"], "notes": it["notes"]}
        for it in items
    ]
    return {"status": "ok", "items": clean_items, "total_price": total}


def _normalize_items(raw_items, lookup: dict) -> List[dict]:
    """Drop unknown items, merge duplicates, and canonicalize names to the menu.

    Returns [{name, quantity, notes, price}] where `price` is the real menu price
    (kept internally only; the caller strips it before sending to the backend).
    """
    merged: dict = {}

    for raw in raw_items or []:
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in lookup:
            continue
        try:
            qty = int(raw.get("quantity", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, qty)
        notes = (raw.get("notes") or "").strip()
        if key in merged:
            merged[key]["quantity"] += qty
            if notes and notes not in merged[key]["notes"]:
                merged[key]["notes"] = (merged[key]["notes"] + "; " + notes).strip("; ")
        else:
            merged[key] = {
                "name": lookup[key]["name"],
                "quantity": qty,
                "notes": notes,
                "price": lookup[key]["price"],
            }

    return [
        {"name": v["name"], "quantity": v["quantity"], "notes": v["notes"], "price": v["price"]}
        for v in merged.values()
    ]
