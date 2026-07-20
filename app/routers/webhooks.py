from __future__ import annotations

import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import runtime
from ..db import get_db
from ..services import hub

router = APIRouter(prefix="/webhooks")


@router.post("/woo")
async def woo_webhook(request: Request, db: Session = Depends(get_db)):
    """WooCommerce order webhook → create a paid receipt in InvoiceNinja and
    email it to the customer immediately. Unauthenticated; verified via the
    WooCommerce webhook secret (HMAC-SHA256 of the raw body)."""
    if not runtime.get_bool("woo_webhook_enabled"):
        return JSONResponse({"status": "disabled"}, status_code=200)

    raw = await request.body()

    secret = runtime.get("woo_webhook_secret")
    if secret:
        sig = request.headers.get("x-wc-webhook-signature", "")
        expected = base64.b64encode(
            hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        ).decode()
        if not hmac.compare_digest(sig, expected):
            return JSONResponse({"status": "invalid signature"}, status_code=401)

    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"status": "bad json"}, status_code=200)

    order_id = data.get("id")
    if not order_id:  # WooCommerce ping / non-order payload
        return JSONResponse({"status": "ignored"}, status_code=200)

    # Only act on the configured (paid) statuses, if the payload carries one.
    status = data.get("status")
    allowed = [s.strip() for s in runtime.get("woo_order_statuses").split(",") if s.strip()]
    if status and allowed and status not in allowed:
        return JSONResponse({"status": "skipped", "order_status": status}, status_code=200)

    try:
        link = hub.fulfill_order_as_receipt(db, int(order_id))
        return JSONResponse(
            {"status": "ok", "invoice": link.invoice_number or link.invoiceninja_id},
            status_code=200,
        )
    except Exception as e:  # noqa: BLE001 - never make Woo retry-storm
        return JSONResponse({"status": "error", "detail": str(e)[:200]}, status_code=200)
