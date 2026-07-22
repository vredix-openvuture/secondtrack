"""InvoiceNinja integration (the invoicing engine / single source of truth).

Enable via SECONDTRACK_INVOICENINJA_ENABLED=1 and provide
SECONDTRACK_INVOICENINJA_URL and SECONDTRACK_INVOICENINJA_TOKEN
(API token from InvoiceNinja → Settings → Account Management → API Tokens).

secondtrack never generates invoices itself; it asks InvoiceNinja to create
and email them, so numbering, PDF, tax and e-invoice (ZUGFeRD) compliance all
stay in InvoiceNinja. This module is the only place that talks HTTP to IN.
"""
from __future__ import annotations

from datetime import date

import httpx
from sqlalchemy.orm import Session

from ... import runtime
from ...models import Project
from ..finance import compute_project


def is_enabled() -> bool:
    return bool(
        runtime.get_bool("in_enabled")
        and runtime.get("in_url")
        and runtime.get("in_token")
    )


def base_url() -> str:
    return runtime.get("in_url").rstrip("/")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=base_url() + "/api/v1",
        headers={
            "X-Api-Token": runtime.get("in_token"),
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=20.0,
    )


def download_pdf(invoice_id: str) -> bytes | None:
    """Best-effort fetch of an invoice PDF from InvoiceNinja."""
    if not is_enabled():
        return None
    try:
        with _client() as c:
            r = c.get(f"/invoices/{invoice_id}/download")
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                return r.content
    except Exception:  # noqa: BLE001
        return None
    return None


def _require() -> None:
    if not is_enabled():
        raise RuntimeError("InvoiceNinja integration is disabled")


# ---- Reads ----

def list_invoices(limit: int = 100, include_archived: bool = False) -> list[dict]:
    _require()
    with _client() as c:
        resp = c.get(
            "/invoices",
            params={"include": "client", "per_page": limit, "sort": "date|desc"},
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    # Deleted invoices are never shown/counted. Archived ones only on request.
    out = []
    for inv in data:
        if inv.get("is_deleted"):
            continue
        if inv.get("archived_at") and not include_archived:
            continue
        out.append(inv)
    return out


def get_invoice(invoice_id: str) -> dict:
    _require()
    with _client() as c:
        r = c.get(f"/invoices/{invoice_id}", params={"include": "client"})
        r.raise_for_status()
        return r.json().get("data", {})


def invoice_recipient(inv: dict) -> tuple[str, str]:
    """Return (email, display_name) for an invoice payload (include=client)."""
    client = inv.get("client") or {}
    name = client.get("display_name") or client.get("name") or ""
    email = ""
    for contact in client.get("contacts", []) or []:
        if contact.get("email"):
            email = contact["email"]
            if not name:
                name = f"{contact.get('first_name','')} {contact.get('last_name','')}".strip()
            break
    return email, name


def invoice_public_link(inv: dict) -> str:
    for inv_invite in inv.get("invitations", []) or []:
        if inv_invite.get("link"):
            return inv_invite["link"]
    return ""


def _invoice_date(inv: dict):
    raw = (inv.get("date") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def filter_period(invoices: list[dict], period: str) -> list[dict]:
    """Filter invoices by their date. period: 'all' | 'year' | 'month'."""
    if period not in ("year", "month"):
        return invoices
    today = date.today()
    out = []
    for inv in invoices:
        d = _invoice_date(inv)
        if d is None:
            continue
        if period == "year" and d.year == today.year:
            out.append(inv)
        elif period == "month" and d.year == today.year and d.month == today.month:
            out.append(inv)
    return out


def get_company_totals(period: str = "all") -> dict:
    """Light KPI snapshot derived from the invoice list. Archived invoices are
    included (a completed/paid invoice is often archived) and can be narrowed
    to the current year/month."""
    invoices = filter_period(list_invoices(limit=400, include_archived=True), period)
    paid = outstanding = draft = 0.0
    for inv in invoices:
        amount = float(inv.get("amount") or 0)
        balance = float(inv.get("balance") or 0)
        status_id = str(inv.get("status_id"))
        if status_id == "1":  # draft
            draft += amount
        else:
            paid += amount - balance
            outstanding += balance
    return {
        # Count real invoices only — empty leftover drafts (status 1) are noise
        # and are hidden from the list too, so they must not inflate the count.
        "count": sum(1 for inv in invoices if str(inv.get("status_id")) != "1"),
        "draft_count": sum(1 for inv in invoices if str(inv.get("status_id")) == "1"),
        "paid": round(paid, 2),
        "outstanding": round(outstanding, 2),
        "draft": round(draft, 2),
    }


# ---- Clients ----

def list_clients(limit: int = 200) -> list[dict]:
    """Existing clients as [{id, name}] for selection dropdowns."""
    _require()
    with _client() as c:
        resp = c.get("/clients", params={"per_page": limit, "sort": "name|asc"})
        resp.raise_for_status()
        out = []
        for cl in resp.json().get("data", []):
            if cl.get("is_deleted"):
                continue
            name = cl.get("display_name") or cl.get("name")
            if not name:
                contacts = cl.get("contacts") or []
                if contacts:
                    name = f"{contacts[0].get('first_name','')} {contacts[0].get('last_name','')}".strip() or contacts[0].get("email")
            out.append({"id": cl["id"], "name": name or cl["id"]})
        return out


def find_or_create_client(
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    address: dict | None = None,
) -> str:
    """Return an InvoiceNinja client id, reusing an existing client matched by
    email where possible, otherwise creating a new one (with address if given).

    `address` keys: address1, city, postal_code, state, country, phone.
    """
    _require()
    with _client() as c:
        if email:
            resp = c.get("/clients", params={"filter": email, "per_page": 50})
            resp.raise_for_status()
            for cl in resp.json().get("data", []):
                if cl.get("is_deleted") or cl.get("archived_at"):
                    continue  # a deleted/archived client can't be used on a new invoice
                for contact in cl.get("contacts", []):
                    if (contact.get("email") or "").lower() == email.lower():
                        return cl["id"]

        a = address or {}
        payload: dict = {
            "name": company or f"{first_name} {last_name}".strip() or email,
            "contacts": [
                {"first_name": first_name, "last_name": last_name, "email": email}
            ],
            "address1": a.get("address1", ""),
            "city": a.get("city", ""),
            "postal_code": a.get("postal_code", ""),
            "state": a.get("state", ""),
            "country_id": a.get("country_id", ""),
            "phone": a.get("phone", ""),
        }
        resp = c.post("/clients", json={k: v for k, v in payload.items() if v != ""} | {"contacts": payload["contacts"]})
        resp.raise_for_status()
        return resp.json()["data"]["id"]


# ---- Invoices ----

def create_invoice(
    client_id: str,
    line_items: list[dict],
    po_number: str = "",
    notes: str = "",
) -> dict:
    _require()
    payload = {
        "client_id": client_id,
        "date": date.today().isoformat(),
        "po_number": po_number,
        "public_notes": notes,
        "line_items": line_items,
    }
    with _client() as c:
        resp = c.post("/invoices", json=payload)
        resp.raise_for_status()
        return resp.json()["data"]


def email_invoice(invoice_id: str) -> None:
    """Ask InvoiceNinja to email the invoice to the client (uses IN's own SMTP)."""
    _require()
    with _client() as c:
        resp = c.post(
            "/invoices/bulk",
            json={"action": "email", "ids": [invoice_id]},
        )
        resp.raise_for_status()


def _bulk(invoice_id: str, action: str) -> None:
    _require()
    with _client() as c:
        r = c.post("/invoices/bulk", json={"action": action, "ids": [invoice_id]})
        r.raise_for_status()


def mark_sent(invoice_id: str) -> None:
    """Mark an invoice as sent (so it leaves draft state)."""
    _bulk(invoice_id, "mark_sent")


def mark_paid(invoice_id: str) -> None:
    """Mark an invoice as paid (records a payment → counts as income).
    Turns the invoice into a paid document = receipt."""
    _bulk(invoice_id, "mark_paid")


def record_payment(invoice_id: str, client_id: str, amount: float,
                   send_email: bool = False) -> dict:
    """Record a payment against an invoice (→ paid + income). With send_email=True
    InvoiceNinja emails the client a payment receipt using IN's *Payment* template
    (separate from the invoice template)."""
    _require()
    payload = {
        "client_id": client_id,
        "amount": round(amount, 2),
        "date": date.today().isoformat(),
        "invoices": [{"invoice_id": invoice_id, "amount": round(amount, 2)}],
        "send_email": bool(send_email),
    }
    with _client() as c:
        r = c.post("/payments", json=payload)
        r.raise_for_status()
        return r.json()["data"]


def send_email(invoice_id: str, template: str = "invoice") -> None:
    """Have InvoiceNinja send an email for the invoice using a given template
    (invoice / reminder1 / reminder2 / reminder3). Uses IN's own SMTP.
    The /emails endpoint expects template names prefixed with 'email_template_'."""
    _require()
    tpl = template if template.startswith("email_template_") else f"email_template_{template}"
    with _client() as c:
        resp = c.post(
            "/emails",
            json={"entity": "invoice", "entity_id": invoice_id, "template": tpl},
        )
        resp.raise_for_status()


def _line_items_for_project(db: Session, project: Project) -> list[dict]:
    f = compute_project(db, project)
    items: list[dict] = []
    # Devices (the refurbished units) at their sale value.
    for d in project.devices:
        if d.sale_price:
            items.append(
                {
                    "product_key": d.name,
                    "notes": "Gerät",
                    "quantity": 1,
                    "cost": round(d.sale_price or 0.0, 2),
                }
            )
    for p in f.parts:
        items.append(
            {
                "product_key": p.name,
                "notes": p.notes or "",
                "quantity": 1,
                "cost": round(p.sale_price or 0.0, 2),
            }
        )
    if f.hours > 0:
        items.append(
            {
                "product_key": "Arbeitszeit",
                "notes": f"{f.hours:.2f} h",
                "quantity": round(f.hours, 2),
                "cost": round(f.rate, 2),
            }
        )
    return items


def line_items_for_project(db: Session, project: Project) -> list[dict]:
    return _line_items_for_project(db, project)


def upcoming_invoice_number() -> str | None:
    return None


# ---- Expenses ----

def _find_or_create(endpoint: str, name: str) -> str | None:
    if not name:
        return None
    with _client() as c:
        r = c.get(f"/{endpoint}", params={"filter": name, "per_page": 50})
        if r.status_code == 200:
            for row in r.json().get("data", []):
                if row.get("is_deleted") or row.get("archived_at"):
                    continue
                if (row.get("name") or "").strip().lower() == name.strip().lower():
                    return row["id"]
        r = c.post(f"/{endpoint}", json={"name": name})
        if r.status_code in (200, 201):
            return r.json()["data"]["id"]
    return None


def create_expense(
    amount: float, date_iso: str, notes: str = "",
    category: str = "", vendor: str = "",
) -> dict:
    _require()
    payload: dict = {"amount": round(amount, 2), "date": date_iso, "public_notes": notes}
    cat_id = _find_or_create("expense_categories", category) if category else None
    ven_id = _find_or_create("vendors", vendor) if vendor else None
    if cat_id:
        payload["category_id"] = cat_id
    if ven_id:
        payload["vendor_id"] = ven_id
    with _client() as c:
        r = c.post("/expenses", json=payload)
        r.raise_for_status()
        return r.json()["data"]


def update_expense(
    expense_id: str, amount: float, date_iso: str,
    notes: str = "", category: str = "", vendor: str = "",
) -> dict:
    _require()
    payload: dict = {"amount": round(amount, 2), "date": date_iso, "public_notes": notes}
    cat_id = _find_or_create("expense_categories", category) if category else None
    ven_id = _find_or_create("vendors", vendor) if vendor else None
    if cat_id:
        payload["category_id"] = cat_id
    if ven_id:
        payload["vendor_id"] = ven_id
    with _client() as c:
        r = c.put(f"/expenses/{expense_id}", json=payload)
        r.raise_for_status()
        return r.json()["data"]


def delete_expense(expense_id: str) -> None:
    _require()
    with _client() as c:
        r = c.post("/expenses/bulk", json={"action": "delete", "ids": [expense_id]})
        r.raise_for_status()


def upload_expense_document(expense_id: str, filename: str, data: bytes) -> bool:
    """Attach a receipt document to an InvoiceNinja expense (best-effort)."""
    if not is_enabled():
        return False
    headers = {
        "X-Api-Token": runtime.get("in_token"),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(base_url=base_url() + "/api/v1", headers=headers, timeout=30.0) as c:
            # InvoiceNinja v5 attaches documents via the entity update with
            # Laravel method spoofing (_method=PUT), not a /upload route.
            r = c.post(
                f"/expenses/{expense_id}",
                data={"_method": "PUT"},
                files={"documents[]": (filename, data)},
            )
            return r.status_code in (200, 201)
    except Exception:  # noqa: BLE001
        return False
