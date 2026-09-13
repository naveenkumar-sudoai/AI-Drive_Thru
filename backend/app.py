"""AI Drive-Thru backend — FastAPI + SQLite + WebSockets.

Single process, single port. Serves:
  - the JSON API used by the voice pipeline and the dashboard
  - a WebSocket at /ws for live order updates
  - the dashboard static files (/, /style.css, /app.js)
  - uploaded order photos (served under /photos)

Run directly:   uvicorn backend.app:app --host 0.0.0.0 --port 8000
or via:         ./run.sh
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db
from .models import MenuItem, MenuItemCreate, MenuItemUpdate, OrderCreate, OrderStatusUpdate

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
PHOTOS_DIR = PROJECT_ROOT / "data" / "photos"

# Ensure directories exist before StaticFiles mounts them.
db.init_db()
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI Drive-Thru", version="1.0.0")

# The dashboard and voice pipeline both hit this app from localhost / the LAN.
# CORS is permissive because everything is on a trusted local network with no auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- WebSocket broadcast manager ----------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to every connected client, dropping dead ones."""
        if not self.active:
            return
        text = json.dumps(message)
        dead: List[WebSocket] = []
        for ws in list(self.active):  # snapshot: a concurrent disconnect may mutate self.active
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# order_ids become both the SQLite PK and the photo filename, so constrain the
# charset to keep a hostile/buggy client from escaping data/photos via "../".
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --- API: menu ----------------------------------------------------------

@app.get("/menu", response_model=List[MenuItem])
async def get_menu():
    return db.list_menu()


@app.post("/menu", response_model=MenuItem, status_code=201)
async def add_menu_item(payload: MenuItemCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Menu item name cannot be empty")
    try:
        return db.create_menu_item(name, payload.price, payload.available)
    except Exception as exc:  # UNIQUE constraint -> duplicate name
        raise HTTPException(status_code=409, detail=f"Menu item '{name}' already exists") from exc


@app.put("/menu/{item_id}", response_model=MenuItem)
async def update_menu_item(item_id: int, payload: MenuItemUpdate):
    updated = db.update_menu_item(
        item_id,
        name=payload.name.strip() if payload.name is not None else None,
        price=payload.price,
        available=payload.available,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Menu item not found")
    return updated


@app.delete("/menu/{item_id}")
async def remove_menu_item(item_id: int):
    if not db.delete_menu_item(item_id):
        raise HTTPException(status_code=404, detail="Menu item not found")
    return {"ok": True}


# --- API: orders --------------------------------------------------------

def _validate_status(status: str) -> str:
    """Return a normalized status or raise 422 for an unrecognized value."""
    s = (status or "").strip().lower()
    if s not in db.VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status!r}")
    return s


@app.post("/order", status_code=201)
async def create_order(payload: OrderCreate):
    order_id = payload.order_id.strip()
    if not order_id:
        raise HTTPException(status_code=422, detail="order_id is required")
    if not ORDER_ID_RE.match(order_id):
        raise HTTPException(status_code=422, detail="order_id may only contain letters, digits, '_' and '-'")
    items = [item.model_dump() for item in payload.items]
    status = payload.status
    status = "pending" if not status or not status.strip() else _validate_status(status)
    try:
        order = db.create_order(
            order_id=order_id,
            items=items,
            total_price=payload.total_price,
            photo_path=payload.photo_path,
            timestamp=payload.timestamp,
            status=status,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"Order '{order_id}' already exists") from exc
    await manager.broadcast({"type": "new_order", "order": order})
    return order


@app.get("/orders")
async def list_orders(status: Optional[str] = None):
    statuses = None
    if status:
        statuses = [s.strip().lower() for s in status.split(",") if s.strip()]
        statuses = [s for s in statuses if s in db.VALID_STATUSES]
    return db.list_orders(statuses)


@app.patch("/orders/{order_id}")
async def update_order_status(order_id: str, payload: OrderStatusUpdate):
    new_status = _validate_status(payload.status)
    updated = db.update_order_status(order_id, new_status)
    if updated is None:
        raise HTTPException(status_code=404, detail="Order not found")
    await manager.broadcast({"type": "order_updated", "order": updated})
    return updated


@app.post("/upload_photo/{order_id}")
async def upload_photo(order_id: str, file: UploadFile = File(...)):
    if not ORDER_ID_RE.match(order_id):
        raise HTTPException(status_code=400, detail="Invalid order_id")
    # Reject non-image content types defensively; allow common ones through.
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Uploaded file must be an image")

    path = PHOTOS_DIR / f"{order_id}.jpg"
    with path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    photo_path = f"photos/{order_id}.jpg"
    updated = db.set_order_photo(order_id, photo_path)
    if updated is not None:
        await manager.broadcast({"type": "order_updated", "order": updated})
    return {"order_id": order_id, "photo_path": photo_path}


# --- API: stats ---------------------------------------------------------

@app.get("/stats")
async def stats():
    return db.get_stats()


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- WebSocket ----------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Keep the socket open; no per-connection protocol needed. We only
        # push broadcasts (new_order / order_updated) as they happen.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# --- Static files (dashboard + photos) -----------------------------------

# Photos must be mounted BEFORE the catch-all dashboard mount so /photos/* is
# served as files rather than falling through to the SPA's 404.
app.mount("/photos", StaticFiles(directory=str(PHOTOS_DIR)), name="photos")
app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")
