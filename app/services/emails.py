"""Compose & send invoice / reminder / dunning emails through secondtrack's
own SMTP, using the editable templates. Pulls invoice data + PDF from
InvoiceNinja."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from .. import runtime
from ..db import get_setting
from ..models import OrderInvoice
from . import mailer
from .integrations import invoiceninja

_TPL = {
    "invoice": ("tpl_invoice_subject", "tpl_invoice_body"),
    "reminder": ("tpl_reminder_subject", "tpl_reminder_body"),
    "dunning": ("tpl_dunning_subject", "tpl_dunning_body"),
    "receipt": ("tpl_receipt_subject", "tpl_receipt_body"),
}

# Map our kinds to InvoiceNinja email templates.
_IN_TEMPLATE = {
    "invoice": "invoice", "reminder": "reminder1",
    "dunning": "reminder3", "receipt": "invoice",
}


def provider() -> str:
    return runtime.get("email_provider") or "secondtrack"


def sending_enabled() -> bool:
    """Whether the chosen email provider is ready to send."""
    if provider() == "invoiceninja":
        return invoiceninja.is_enabled()
    return mailer.is_configured()


def _deliver(db: Session, invoice_id: str, kind: str) -> None:
    """Send one email for an InvoiceNinja invoice via the configured provider."""
    if provider() == "invoiceninja":
        invoiceninja.send_email(invoice_id, _IN_TEMPLATE.get(kind, "invoice"))
        return
    # secondtrack's own SMTP
    inv = invoiceninja.get_invoice(invoice_id)
    to_email, subject, body = render(db, kind, inv)
    attachments = []
    if kind in ("invoice", "receipt"):
        pdf = invoiceninja.download_pdf(invoice_id)
        if pdf:
            label = "receipt" if kind == "receipt" else "invoice"
            attachments.append((f"{inv.get('number', label)}.pdf", pdf, "pdf"))
    mailer.send(to_email, subject, body, attachments)
    # A plain invoice we emailed ourselves should leave draft state.
    if kind == "invoice":
        try:
            invoiceninja.mark_sent(invoice_id)
        except Exception:  # noqa: BLE001
            pass


class _Safe(dict):
    def __missing__(self, key):  # noqa: D401
        return ""


def _context(db: Session, inv: dict) -> dict:
    email, name = invoiceninja.invoice_recipient(inv)
    currency = get_setting(db, "currency", "€")
    amount = float(inv.get("amount") or 0)
    return {
        "_email": email,
        "client": name or email,
        "number": inv.get("number", ""),
        "amount": f"{amount:.2f} {currency}",
        "due_date": inv.get("due_date") or "",
        "link": invoiceninja.invoice_public_link(inv),
        "company": runtime.get("mail_from_name") or "secondtrack",
    }


def render(db: Session, kind: str, inv: dict) -> tuple[str, str, str]:
    ctx = _context(db, inv)
    subj_key, body_key = _TPL[kind]
    subject = runtime.get(subj_key).format_map(_Safe(ctx))
    body = runtime.get(body_key).format_map(_Safe(ctx))
    return ctx["_email"], subject, body


def send_for_link(db: Session, link: OrderInvoice, kind: str = "invoice") -> None:
    """Send an email for the given invoice link via the configured provider."""
    _deliver(db, link.invoiceninja_id, kind)
    now = datetime.utcnow()
    if kind in ("invoice", "receipt"):
        link.emailed_at = now
    elif kind == "reminder":
        link.reminder_sent_at = now
    elif kind == "dunning":
        link.dunning_sent_at = now
    db.commit()


def send_by_invoice_id(db: Session, invoice_id: str, kind: str = "reminder") -> None:
    """Ad-hoc send for an InvoiceNinja invoice id (no local link tracking)."""
    _deliver(db, invoice_id, kind)


def process_due(db: Session) -> dict:
    """Auto-send reminders/dunning for overdue, unpaid invoices.
    Returns a small summary. Each stage is sent at most once per invoice."""
    if not invoiceninja.is_enabled() or not sending_enabled():
        return {"reminders": 0, "dunning": 0, "skipped": "not configured"}

    reminder_days = runtime.get_int("reminder_days", 0)
    dunning_days = runtime.get_int("dunning_days", 30)
    today = date.today()
    sent_r = sent_d = 0

    links = db.query(OrderInvoice).filter(OrderInvoice.invoiceninja_id != "").all()
    for link in links:
        try:
            inv = invoiceninja.get_invoice(link.invoiceninja_id)
        except Exception:  # noqa: BLE001
            continue
        if inv.get("is_deleted"):
            continue
        balance = float(inv.get("balance") or 0)
        due = inv.get("due_date")
        if balance <= 0 or not due:
            continue
        try:
            days = (today - date.fromisoformat(due)).days
        except ValueError:
            continue
        if days >= dunning_days and not link.dunning_sent_at:
            try:
                send_for_link(db, link, "dunning")
                sent_d += 1
            except Exception:  # noqa: BLE001
                pass
        elif days >= reminder_days and not link.reminder_sent_at:
            try:
                send_for_link(db, link, "reminder")
                sent_r += 1
            except Exception:  # noqa: BLE001
                pass
    return {"reminders": sent_r, "dunning": sent_d}
