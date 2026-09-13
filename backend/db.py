"""SQLite storage layer for the AI Drive-Thru backend.

Uses plain sqlite3 (no ORM) to keep the Raspberry Pi dependency surface small.
Connections are opened per-operation and WAL mode is enabled so the single
FastAPI process (plus the voice pipeline, if it ever reads the DB directly)
can safely read while a write is in flight.

The database file lives at data/app.db; order photos at data/photos/.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = Path(os.environ.get("AI_DRIVE_THRU_DB", str(DATA_DIR / "app.db")))

VALID_STATUSES = ("pending", "preparing", "ready", "picked_up")


# --- low-level helpers --------------------------------------------------

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create the data directories and tables if they don't exist yet."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS menu_items (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL UNIQUE,
                price     REAL    NOT NULL,
                available INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS orders (
                id          TEXT PRIMARY KEY,
                timestamp   REAL NOT NULL,
                items_json  TEXT NOT NULL,
                total_price REAL NOT NULL,
                photo_path  TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _local_midnight_unix() -> float:
    """Unix timestamp of the most recent local midnight."""
    today = datetime.datetime.now().date()
    return time.mktime(today.timetuple())


# --- menu ---------------------------------------------------------------

def _menu_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "price": row["price"],
        "available": bool(row["available"]),
    }


def list_menu() -> List[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, price, available FROM menu_items ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [_menu_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_menu_item(item_id: int) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, name, price, available FROM menu_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return _menu_to_dict(row) if row else None
    finally:
        conn.close()


def create_menu_item(name: str, price: float, available: bool) -> dict[str, Any]:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO menu_items (name, price, available) VALUES (?, ?, ?)",
            (name, price, int(available)),
        )
        conn.commit()
        return get_menu_item(cur.lastrowid)  # type: ignore[arg-type]
    finally:
        conn.close()


def update_menu_item(
    item_id: int,
    name: Optional[str] = None,
    price: Optional[float] = None,
    available: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        sets: List[str] = []
        params: List[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if price is not None:
            sets.append("price = ?")
            params.append(price)
        if available is not None:
            sets.append("available = ?")
            params.append(int(available))
        if sets:
            params.append(item_id)
            conn.execute(f"UPDATE menu_items SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
        return get_menu_item(item_id)
    finally:
        conn.close()


def delete_menu_item(item_id: int) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- orders -------------------------------------------------------------

def _order_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "items": json.loads(row["items_json"]),
        "total_price": row["total_price"],
        "photo_path": row["photo_path"],
        "status": row["status"],
    }


def get_order(order_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return _order_to_dict(row) if row else None
    finally:
        conn.close()


def create_order(
    order_id: str,
    items: List[dict],
    total_price: float,
    photo_path: Optional[str],
    timestamp: Optional[float],
    status: str,
) -> dict[str, Any]:
    conn = _conn()
    try:
        ts = timestamp if timestamp is not None else time.time()
        conn.execute(
            "INSERT INTO orders (id, timestamp, items_json, total_price, photo_path, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, ts, json.dumps(items), total_price, photo_path, status),
        )
        conn.commit()
        return get_order(order_id)  # type: ignore[return-value]
    finally:
        conn.close()


def list_orders(statuses: Optional[List[str]] = None) -> List[dict[str, Any]]:
    """List orders, most recent first. Optionally filtered to a set of statuses."""
    conn = _conn()
    try:
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            rows = conn.execute(
                f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY timestamp DESC",
                list(statuses),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders ORDER BY timestamp DESC").fetchall()
        return [_order_to_dict(r) for r in rows]
    finally:
        conn.close()


def update_order_status(order_id: str, status: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return get_order(order_id)
    finally:
        conn.close()


def set_order_photo(order_id: str, photo_path: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE orders SET photo_path = ? WHERE id = ?", (photo_path, order_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return get_order(order_id)
    finally:
        conn.close()


# --- stats --------------------------------------------------------------

def _top_items(rows: List[sqlite3.Row], n: int = 5) -> List[dict[str, Any]]:
    """Aggregate quantity sold per item name across a set of order rows."""
    counts: dict[str, int] = {}
    for row in rows:
        for item in json.loads(row["items_json"]):
            qty = int(item.get("quantity", 1) or 1)
            counts[item["name"]] = counts.get(item["name"], 0) + qty
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:n]
    return [{"name": name, "quantity": qty} for name, qty in top]


def get_stats() -> dict[str, Any]:
    """Return dashboard analytics: today's totals, top items (today + all-time),
    and an orders-per-hour histogram for today."""
    conn = _conn()
    try:
        all_rows = conn.execute("SELECT * FROM orders").fetchall()
        today_start = _local_midnight_unix()
        today_rows = conn.execute(
            "SELECT * FROM orders WHERE timestamp >= ?", (today_start,)
        ).fetchall()
    finally:
        conn.close()

    total_orders_today = len(today_rows)
    total_revenue_today = round(sum(r["total_price"] for r in today_rows), 2)

    orders_per_hour = [0] * 24
    for row in today_rows:
        hour = datetime.datetime.fromtimestamp(row["timestamp"]).hour
        orders_per_hour[hour] += 1

    return {
        "total_orders_today": total_orders_today,
        "total_revenue_today": total_revenue_today,
        "top_items_today": _top_items(today_rows),
        "top_items_all_time": _top_items(all_rows),
        "orders_per_hour": orders_per_hour,
    }
