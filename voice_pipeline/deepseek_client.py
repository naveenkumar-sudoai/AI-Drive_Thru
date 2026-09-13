"""DeepSeek order parsing for the AI Drive-Thru.

The menu is loaded LIVE from the backend (GET /menu) so the dashboard's Menu
Editor is the single source of truth. If the backend is unreachable, it falls
back to a local menu.json.

The key job here is to let the LLM do the heavy lifting — understand slang,
abbreviations and context ("ghee roast" -> "Ghee Roast Dosa", "kaapi" ->
"Filter Coffee") — and then only lightly reconcile its answer against the menu
rather than hard-dropping anything that isn't an exact string match.
"""

from __future__ import annotations

import json
import os
import re
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
    """Fetch the current menu from the backend; fall back to local menu.json."""
    try:
        resp = requests.get(f"{BACKEND_URL}/menu", timeout=3)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return _load_menu_from_file()


def _load_menu_from_file() -> List[dict]:
    items = []
    try:
        with open(MENU_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return items
    for it in data if isinstance(data, list) else data.get("items", []):
        if isinstance(it, dict) and it.get("name"):
            items.append(
                {
                    "name": it.get("name"),
                    "price": float(it.get("price", 0)),
                    "available": bool(it.get("available", True)),
                }
            )
    return items


def _menu_text(menu: List[dict]) -> str:
    lines = [
        f"- {item.get('name')}: ₹{float(item.get('price', 0)):.2f}"
        for item in menu
        if item.get("available", True)
    ]
    return "\n".join(lines) if lines else "(no menu items available)"


# --- order parsing ------------------------------------------------------

_SYSTEM_PROMPT = """You are the order-taking assistant for a South Indian drive-thru restaurant.
Parse the customer's spoken order (English or Tamil; may use slang, abbreviations
or casual phrasing) into structured JSON.

Menu — these are the ONLY items and their exact names/prices:
{menu}

How to understand the customer (use context and the menu, not exact-word matching):
- Map slang/abbreviations to the EXACT menu item. Examples: "ghee roast" -> "Ghee Roast Dosa",
  "kaapi"/"filter kaapi"/"degree coffee"/"by two" -> "Filter Coffee", "sambar vadai" -> "Medu Vada"
  (note "with sambar"), "combo" -> "Idli Vada Combo", "pongal" -> "Ven Pongal", "upma" -> "Rava Upma".
- Customers often drop words: "two ghee roast" = 2 x Ghee Roast Dosa, "one vada" = 1 x Medu Vada.
- Sizes/modifiers go in "notes": "no onion", "extra sambar", "less spicy", "no sugar", "large", "one by two".
- Use the menu item's EXACT spelling/capitalization as listed above.
- Combine duplicate items by summing their quantities.

When to ask a clarification (ONE short, friendly question in the customer's language):
- The request is ambiguous between 2+ menu items (e.g. just "dosa" or "coffee") -> list the specific options.
- An item is requested that is not on the menu and has no clear match -> say it's not available and suggest the closest item.
- The order is empty or you cannot tell what they want.

Respond with ONLY a JSON object (no markdown, no prose) in exactly one of these shapes:

Valid order:
{{"status":"ok","items":[{{"name":"<EXACT menu name>","quantity":<int>,"notes":"<string or empty>"}}],"total_price":<number>}}

Clarification:
{{"status":"clarification","question":"<short question>"}}"""


def _norm(s: str) -> str:
    """Normalize a name for tolerant matching (lowercase, no punctuation)."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _menu_lookup(menu: List[dict]) -> dict:
    """Map normalized menu name -> {"name": canonical, "price": float} (available only)."""
    lookup = {}
    for item in menu:
        if not item.get("available", True):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        lookup[_norm(name)] = {"name": name, "price": float(item.get("price", 0))}
    return lookup


def _match_item(name: str, lookup: dict) -> Optional[dict]:
    """Map a (possibly sloppy) item name to a menu entry, or None if ambiguous."""
    n = _norm(name)
    if not n:
        return None
    if n in lookup:
        return lookup[n]
    # Partial match: the model may return a shorter form of a menu name.
    candidates = [v for k, v in lookup.items() if n in k]
    if len(candidates) == 1:
        return candidates[0]
    return None


def parse_order(transcript: str, menu: List[dict]) -> dict:
    """Parse a transcript into a structured order using DeepSeek.

    Returns {"status": "ok", "items": [...], "total_price": <float>}
         or {"status": "clarification", "question": str}.
    """
    if not transcript.strip():
        return {
            "status": "clarification",
            "question": "Sorry, I didn't catch that. Could you repeat your order?",
        }

    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Export it before running the voice pipeline."
        )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT.format(menu=_menu_text(menu))},
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
        return {
            "status": "clarification",
            "question": "Sorry, I had trouble understanding that. Could you repeat your order?",
        }

    if result.get("status") == "clarification":
        return result

    # Reconcile the model's items against the real menu (tolerant matching) and
    # recompute the total from real prices so we never trust a hallucinated one.
    lookup = _menu_lookup(menu)
    items = _normalize_items(result.get("items", []), lookup)
    if not items:
        return {
            "status": "clarification",
            "question": "Sorry, I couldn't find those items on the menu. What would you like?",
        }

    total = round(sum(it["price"] * it["quantity"] for it in items), 2)
    clean_items = [
        {"name": it["name"], "quantity": it["quantity"], "notes": it["notes"]}
        for it in items
    ]
    return {"status": "ok", "items": clean_items, "total_price": total}


def _normalize_items(raw_items, lookup: dict) -> List[dict]:
    """Drop unknown items, merge duplicates, canonicalize names, keep real prices."""
    merged: dict = {}

    for raw in raw_items or []:
        name = (raw.get("name") or "").strip()
        entry = _match_item(name, lookup)
        if entry is None:
            continue
        key = _norm(entry["name"])
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
                "name": entry["name"],
                "quantity": qty,
                "notes": notes,
                "price": entry["price"],
            }

    return [
        {"name": v["name"], "quantity": v["quantity"], "notes": v["notes"], "price": v["price"]}
        for v in merged.values()
    ]


def summarize_order(items: List[dict], total: float) -> str:
    """Human-readable one-line order summary for TTS, display and confirmation."""
    parts = []
    for it in items:
        q = int(it.get("quantity", 1))
        name = it.get("name", "?")
        notes = (it.get("notes") or "").strip()
        s = f"{q} {name}"
        if notes:
            s += f" ({notes})"
        parts.append(s)
    body = ", ".join(parts) if parts else "nothing"
    return f"{body}. Total {total:.2f} rupees."


_CONFIRM_PROMPT = """You are the order-confirmation step of a drive-thru assistant.
The customer was told their order is: {summary}
The customer then responded: "{response}"

Classify the customer's response into exactly ONE of:
- "yes"     — they confirmed the order is correct (e.g. "yes", "correct", "that's right", "okay", "சரி", "ஆமாம்")
- "no"      — they rejected it outright (e.g. "no", "wrong", "இல்லை")
- "change"  — they want to change part of the order (e.g. "remove the coffee", "make it two dosas", "add one vada")
- "unclear" — you cannot tell what they meant

Respond with ONLY a JSON object: {{"status": "<yes|no|change|unclear>"}}"""


def confirm_order(response: str, summary: str) -> str:
    """Classify the customer's confirmation answer. Returns yes/no/change/unclear."""
    response = (response or "").strip()
    if not response:
        return "unclear"
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Export it before running the voice pipeline."
        )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": _CONFIRM_PROMPT.format(summary=summary, response=response),
            },
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
        status = str(json.loads(content).get("status", "unclear")).lower()
    except json.JSONDecodeError:
        return "unclear"
    return status if status in ("yes", "no", "change") else "unclear"
