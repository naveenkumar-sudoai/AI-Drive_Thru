"""Pydantic request/response schemas for the AI Drive-Thru backend.

These define the exact wire format the voice pipeline and the dashboard
exchange with the API. Keep field names in sync with:
  - voice_pipeline/main.py  (POST /order payload)
  - dashboard/app.js        (fetch bodies + WebSocket message handling)
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# --- Menu ---------------------------------------------------------------

class MenuItem(BaseModel):
    """A menu item as returned by the API (response shape)."""
    id: int
    name: str
    price: float
    available: bool = True


class MenuItemCreate(BaseModel):
    """Body for POST /menu."""
    name: str
    price: float = Field(ge=0)
    available: bool = True


class MenuItemUpdate(BaseModel):
    """Body for PUT /menu/{id}. All fields optional -> partial update."""
    name: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    available: Optional[bool] = None


# --- Orders -------------------------------------------------------------

class OrderItem(BaseModel):
    """A single line item inside an order."""
    name: str
    quantity: int = 1
    notes: Optional[str] = None


class OrderCreate(BaseModel):
    """Body for POST /order.

    `items` is a list of {name, quantity, notes}; the backend serializes it
    into the orders.items_json column. `timestamp` and `status` are optional
    so the voice pipeline can omit them and let the backend default.
    """
    order_id: str
    items: List[OrderItem] = Field(min_length=1)
    total_price: float
    photo_path: Optional[str] = None
    timestamp: Optional[float] = None
    status: str = "pending"


class OrderStatusUpdate(BaseModel):
    """Body for PATCH /orders/{id}."""
    status: str
