"""WooCommerce integration (read orders, optionally push products).

Enable via SECONDTRACK_WOO_ENABLED=1 and provide SECONDTRACK_WOO_URL /
_KEY / _SECRET (a WooCommerce REST API key pair, read access is enough for
the hub). This module is the only place that talks HTTP to WooCommerce.
"""
from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from ... import runtime
from ...models import Project


def is_enabled() -> bool:
    return bool(
        runtime.get_bool("woo_enabled")
        and runtime.get("woo_url")
        and runtime.get("woo_key")
        and runtime.get("woo_secret")
    )


def _status_list() -> list[str]:
    return [s.strip() for s in runtime.get("woo_order_statuses").split(",") if s.strip()]


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=runtime.get("woo_url").rstrip("/") + "/wp-json/wc/v3",
        auth=(runtime.get("woo_key"), runtime.get("woo_secret")),
        timeout=20.0,
    )


def _require() -> None:
    if not is_enabled():
        raise RuntimeError("WooCommerce integration is disabled")


def list_orders(limit: int = 30) -> list[dict]:
    """Recent orders in the configured statuses, newest first."""
    _require()
    params = {
        "per_page": limit,
        "orderby": "date",
        "order": "desc",
        "status": ",".join(_status_list()),
    }
    with _client() as c:
        resp = c.get("/orders", params=params)
        resp.raise_for_status()
        return resp.json()


def get_order(order_id: int) -> dict:
    _require()
    with _client() as c:
        resp = c.get(f"/orders/{order_id}")
        resp.raise_for_status()
        return resp.json()


def order_to_invoice_inputs(order: dict) -> tuple[dict, list[dict], str]:
    """Map a Woo order to (client_kwargs, line_items, po_number)."""
    billing = order.get("billing", {}) or {}
    client_kwargs = {
        "email": billing.get("email", ""),
        "first_name": billing.get("first_name", ""),
        "last_name": billing.get("last_name", ""),
        "company": billing.get("company", ""),
        "address": billing,
    }
    line_items = []
    for li in order.get("line_items", []):
        qty = li.get("quantity", 1) or 1
        total = float(li.get("total") or 0)
        unit = round(total / qty, 2) if qty else total
        line_items.append(
            {
                "product_key": li.get("name", "Artikel"),
                "notes": li.get("sku", "") or "",
                "quantity": qty,
                "cost": unit,
            }
        )
    # Shipping as a line item if present.
    shipping_total = float(order.get("shipping_total") or 0)
    if shipping_total:
        line_items.append(
            {"product_key": "Versand", "notes": "", "quantity": 1, "cost": shipping_total}
        )
    po_number = str(order.get("number") or order.get("id") or "")
    return client_kwargs, line_items, po_number


def upsert_product(db: Session, project: Project) -> dict:
    """Create or update a WooCommerce product for a finished project."""
    _require()
    from ..markdown import render_project_markdown

    from ..finance import compute_project

    f = compute_project(db, project)
    payload = {
        "name": project.name,
        "type": "simple",
        "regular_price": f"{f.sale_price:.2f}",
        "description": render_project_markdown(db, project),
        "status": "draft",
        "manage_stock": True,
        "stock_quantity": 1,
    }
    with _client() as c:
        if project.woo_product_id:
            resp = c.put(f"/products/{project.woo_product_id}", json=payload)
        else:
            resp = c.post("/products", json=payload)
        resp.raise_for_status()
        data = resp.json()
    project.woo_product_id = data.get("id", project.woo_product_id)
    db.commit()
    return data
