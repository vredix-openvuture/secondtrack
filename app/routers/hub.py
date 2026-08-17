from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import runtime
from ..auth import require_login
from ..db import get_db
from ..models import OrderInvoice
from ..services import emails, hub
from ..services.integrations import invoiceninja, nextcloud, vikunja
from ..templating import ctx, templates

router = APIRouter(prefix="/hub")


@router.get("")
def hub_page(
    request: Request,
    drafts: int = 0,
    archived: int = 1,
    period: str = "all",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    include_drafts = bool(drafts)
    include_archived = bool(archived)
    if period not in ("all", "year", "month"):
        period = "all"
    view = hub.build_hub_view(
        db, include_drafts=include_drafts, include_archived=include_archived,
        period=period,
    )
    return templates.TemplateResponse(
        "hub.html",
        ctx(
            request,
            db,
            active="hub",
            view=view,
            include_drafts=include_drafts,
            include_archived=include_archived,
            period=period,
            in_url=invoiceninja.base_url(),
            vikunja_url=vikunja.web_url() if vikunja.is_enabled() else "",
            auto_send=runtime.get_bool("in_auto_send"),
            nc_enabled=nextcloud.is_enabled(),
            email_on=emails.sending_enabled(),
            msg=request.query_params.get("msg"),
        ),
    )


@router.post("/orders/{order_id}/invoice")
def invoice_order(
    order_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    try:
        link = hub.create_invoice_for_order(db, order_id)
        msg = f"Invoice {link.invoice_number or ''} created."
        if link.emailed_at:
            msg += " Sent to customer."
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)


@router.post("/invoice/{link_id}/send")
def send_invoice(
    link_id: int,
    kind: str = "invoice",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    link = db.get(OrderInvoice, link_id)
    if not link:
        return RedirectResponse("/hub?msg=Invoice not found", status_code=303)
    valid = {"invoice", "reminder", "dunning"}
    k = kind if kind in valid else "invoice"
    try:
        hub.send_invoice(db, link, k)
        label = {"invoice": "Invoice", "reminder": "Reminder", "dunning": "Dunning notice"}[k]
        msg = f"{label} for {link.invoice_number or ''} sent to customer."
    except Exception as e:  # noqa: BLE001
        msg = f"Error while sending: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)


@router.post("/invoice/{link_id}/archive")
def archive_invoice(
    link_id: int,
    kind: str = "rechnung",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    link = db.get(OrderInvoice, link_id)
    if not link:
        return RedirectResponse("/hub?msg=Invoice not found", status_code=303)
    try:
        remote = hub.archive_invoice(db, link)
        msg = f"In Nextcloud abgelegt: {remote}"
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)


@router.post("/archive-paid")
def archive_paid(
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    try:
        r = hub.archive_paid_invoices(db)
        up = r.get("updated", 0)
        msg = f"Nextcloud: {r.get('archived', 0)} neu abgelegt" + (
            f", {up} aktualisiert." if up else "."
        )
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)


@router.post("/in/{invoice_id}/mail")
def mail_invoice(
    invoice_id: str,
    kind: str = "reminder",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    from ..services import emails

    k = kind if kind in {"invoice", "reminder", "dunning"} else "reminder"
    try:
        emails.send_by_invoice_id(db, invoice_id, k)
        label = {"invoice": "Invoice", "reminder": "Reminder", "dunning": "Dunning notice"}[k]
        msg = f"{label} sent to customer."
    except Exception as e:  # noqa: BLE001
        msg = f"Error while sending: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)


@router.post("/poll-orders")
def poll_orders(
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    try:
        r = hub.poll_orders(db)
        msg = f"Checked orders: {r.get('fulfilled', 0)} new receipt(s) sent."
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)


@router.post("/process-due")
def process_due(
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    from ..services import emails

    try:
        r = emails.process_due(db)
        msg = f"Processed overdue: {r.get('reminders', 0)} reminders, {r.get('dunning', 0)} dunning."
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/hub?msg={msg}", status_code=303)
