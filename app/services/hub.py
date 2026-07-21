"""Hub aggregation: ties WooCommerce orders, projects and InvoiceNinja
invoices into one coherent business overview, and orchestrates invoice
creation/sending. All invoice generation stays in InvoiceNinja."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime

from sqlalchemy.orm import Session

from .. import runtime
from ..models import InvoiceSource, OrderInvoice, Project, ProjectKind, ProjectStatus
from .integrations import invoiceninja, woo



# ---- Orchestration ----

def create_invoice_for_order(
    db: Session, order_id: int, auto_send: bool = True
) -> OrderInvoice:
    """Create (and optionally email) an InvoiceNinja invoice for a Woo order."""
    existing = (
        db.query(OrderInvoice).filter(OrderInvoice.woo_order_id == order_id).first()
    )
    if existing:
        return existing

    order = woo.get_order(order_id)
    client_kwargs, line_items, po_number = woo.order_to_invoice_inputs(order)
    client_id = invoiceninja.find_or_create_client(**client_kwargs)
    inv = invoiceninja.create_invoice(
        client_id, line_items, po_number=po_number,
        notes=f"WooCommerce Bestellung #{po_number}",
    )

    link = OrderInvoice(
        source=InvoiceSource.woo,
        woo_order_id=order_id,
        invoiceninja_id=inv["id"],
        invoice_number=inv.get("number"),
        amount=float(inv.get("amount") or 0),
        status=str(inv.get("status_id")),
    )
    db.add(link)
    db.commit()

    if auto_send and runtime.get_bool('in_auto_send'):
        send_invoice(db, link)
    return link


def create_invoice_for_project(
    db: Session,
    project: Project,
    client_id: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    address: dict | None = None,
) -> OrderInvoice:
    existing = (
        db.query(OrderInvoice)
        .filter(OrderInvoice.project_id == project.id)
        .first()
    )
    if existing:
        return existing

    # Use an existing InvoiceNinja client if one was picked, else create one.
    if not client_id:
        client_id = invoiceninja.find_or_create_client(
            email=email, first_name=first_name, last_name=last_name,
            company=company, address=address,
        )
    line_items = invoiceninja.line_items_for_project(db, project)
    inv = invoiceninja.create_invoice(
        client_id, line_items, po_number=str(project.id),
        notes=f"secondtrack Projekt: {project.name}",
    )

    project.invoiceninja_id = inv["id"]
    link = OrderInvoice(
        source=InvoiceSource.project,
        project_id=project.id,
        invoiceninja_id=inv["id"],
        invoice_number=inv.get("number"),
        amount=float(inv.get("amount") or 0),
        status=str(inv.get("status_id")),
    )
    db.add(link)
    db.commit()

    if runtime.get_bool('in_auto_send'):
        send_invoice(db, link)
    return link


def fulfill_order_as_receipt(db: Session, order_id: int) -> OrderInvoice:
    """Direct (paid) shop order → create the document in IN, mark it PAID
    (records income, becomes a receipt) and email the receipt to the customer."""
    from . import emails

    import time

    from . import emails

    link = create_invoice_for_order(db, order_id, auto_send=False)

    # Reconcile: if the ordered product was produced in-house (a shop project),
    # link the sale to that project and mark it sold.
    if not link.project_id:
        try:
            order = woo.get_order(order_id)
            pids = [li.get("product_id") for li in (order.get("line_items") or []) if li.get("product_id")]
            if pids:
                proj = (
                    db.query(Project)
                    .filter(
                        Project.kind == ProjectKind.shop,
                        Project.woo_product_id.in_(pids),
                        Project.status != ProjectStatus.sold,
                    )
                    .first()
                )
                if proj:
                    link.project_id = proj.id
                    proj.status = ProjectStatus.sold
                    db.commit()
        except Exception:  # noqa: BLE001
            pass

    if link.emailed_at:  # already handled before
        return link

    inv = invoiceninja.get_invoice(link.invoiceninja_id)
    client_id = inv.get("client_id")
    amount = float(inv.get("balance") or inv.get("amount") or 0)

    if emails.provider() == "invoiceninja":
        # Record the payment AND have IN email the *Payment* receipt template
        # ("thank you for your order") — separate from the invoice template.
        for attempt in range(2):
            try:
                invoiceninja.record_payment(link.invoiceninja_id, client_id, amount, send_email=True)
                from datetime import datetime as _dt
                link.emailed_at = _dt.utcnow()
                db.commit()
                break
            except Exception:  # noqa: BLE001
                if attempt == 0:
                    time.sleep(2)
    else:
        # secondtrack SMTP: mark paid (income) and send our own receipt template.
        try:
            invoiceninja.record_payment(link.invoiceninja_id, client_id, amount, send_email=False)
        except Exception:  # noqa: BLE001
            try:
                invoiceninja.mark_paid(link.invoiceninja_id)
            except Exception:  # noqa: BLE001
                pass
        for attempt in range(2):
            try:
                emails.send_for_link(db, link, "receipt")
                break
            except Exception:  # noqa: BLE001
                if attempt == 0:
                    time.sleep(2)
    return link


def poll_orders(db: Session) -> dict:
    """Find new paid Woo orders (since the watermark, not yet processed) and
    auto-create + send a receipt for each. Safe to run repeatedly (dedupe)."""
    if not woo.is_enabled():
        return {"fulfilled": 0, "skipped": "woo disabled"}

    raw = runtime.get("woo_poll_since")
    if not raw:
        # Safety: never auto-receipt historical orders. The first run only
        # establishes the watermark; only orders placed after this are processed.
        runtime.save(db, {"woo_poll_since": datetime.utcnow().isoformat(timespec="seconds")})
        return {"fulfilled": 0, "note": "watermark initialized"}
    try:
        since = datetime.fromisoformat(raw)
    except ValueError:
        since = None

    allowed = [s.strip() for s in runtime.get("woo_order_statuses").split(",") if s.strip()]
    existing = {
        li.woo_order_id
        for li in db.query(OrderInvoice).filter(OrderInvoice.woo_order_id.isnot(None))
    }

    fulfilled = 0
    try:
        orders = woo.list_orders(limit=50)
    except Exception as e:  # noqa: BLE001
        return {"fulfilled": 0, "error": str(e)[:200]}

    for o in orders:
        oid = o.get("id")
        if not oid or oid in existing:
            continue
        status = o.get("status")
        if allowed and status and status not in allowed:
            continue
        if since:
            dc = (o.get("date_created_gmt") or o.get("date_created") or "").replace("Z", "")
            if dc:
                try:
                    if datetime.fromisoformat(dc) < since:
                        continue
                except ValueError:
                    pass
        try:
            fulfill_order_as_receipt(db, int(oid))
            fulfilled += 1
        except Exception:  # noqa: BLE001
            continue
    return {"fulfilled": fulfilled}


def send_invoice(db: Session, link: OrderInvoice, kind: str = "invoice") -> None:
    """Send invoice/reminder/dunning via secondtrack's own SMTP."""
    from . import emails

    emails.send_for_link(db, link, kind)
    _maybe_archive(db, link)


def _safe_name(s: str) -> str:
    """Filesystem-safe: strip path/illegal chars, collapse whitespace."""
    s = (s or "").strip()
    for ch in '/\\:*?"<>|':
        s = s.replace(ch, "-")
    return " ".join(s.split())


def _invoice_remote_path(inv: dict) -> str:
    """Nextcloud path for an InvoiceNinja invoice dict:
    <base>/Invoices/<year>/<number>_<customer>.pdf"""
    number = _safe_name(str(inv.get("number") or inv.get("id")))
    client = inv.get("client") or {}
    cname = _safe_name(client.get("display_name") or client.get("name") or "")
    d = invoiceninja._invoice_date(inv)
    year = str(d.year) if d else datetime.utcnow().strftime("%Y")
    stem = f"{number}_{cname}".strip("_ ") or number
    base = (runtime.get("nc_base_path") or "/OpenVuture/Belege").rstrip("/")
    return f"{base}/Invoices/{year}/{stem}.pdf"


def archive_invoice(db: Session, link: OrderInvoice, kind: str = "") -> str:
    """Fetch the invoice PDF from InvoiceNinja and store it in Nextcloud under
    Invoices/<year>/<number>_<customer>.pdf. Returns the remote path."""
    from .integrations import nextcloud

    if not nextcloud.is_enabled():
        raise RuntimeError("Nextcloud-Integration ist deaktiviert")
    if not link.invoiceninja_id:
        raise RuntimeError("Keine InvoiceNinja-Rechnung verknüpft")
    pdf = invoiceninja.download_pdf(link.invoiceninja_id)
    if not pdf:
        raise RuntimeError("PDF konnte nicht aus InvoiceNinja geladen werden")
    inv = invoiceninja.get_invoice(link.invoiceninja_id) or {
        "number": link.invoice_number, "id": link.invoiceninja_id,
    }
    return nextcloud.put_file(_invoice_remote_path(inv), pdf, "application/pdf")


def archive_paid_invoices(db: Session) -> dict:
    """Archive every PAID InvoiceNinja invoice not yet stored into Nextcloud
    (Invoices/<year>/<number>_<customer>.pdf). Deduped via a setting so each
    invoice uploads exactly once. Returns {'archived': n}."""
    from ..db import get_setting, set_setting
    from .integrations import nextcloud

    if not nextcloud.is_enabled() or not invoiceninja.is_enabled():
        return {"archived": 0, "skipped": "integration disabled"}

    done = {
        x for x in (get_setting(db, "nc_archived_invoice_ids", "") or "").split(",") if x
    }
    archived = 0
    for inv in invoiceninja.list_invoices(limit=400, include_archived=True):
        if str(inv.get("status_id")) != "4":  # 4 = paid
            continue
        iid = str(inv.get("id"))
        if iid in done:
            continue
        pdf = invoiceninja.download_pdf(iid)
        if not pdf:
            continue
        try:
            nextcloud.put_file(_invoice_remote_path(inv), pdf, "application/pdf")
        except Exception:  # noqa: BLE001
            continue
        done.add(iid)
        archived += 1
    set_setting(db, "nc_archived_invoice_ids", ",".join(sorted(done)))
    return {"archived": archived}


def _maybe_archive(db: Session, link: OrderInvoice) -> None:
    """Auto-archive to Nextcloud after sending, if enabled. Never raises —
    archiving must not break the send path."""
    from .integrations import nextcloud

    if not runtime.get_bool("nc_auto_archive") or not nextcloud.is_enabled():
        return
    try:
        archive_invoice(db, link)
    except Exception:  # noqa: BLE001
        pass


# ---- Overview ----

@dataclass
class HubView:
    woo_enabled: bool
    in_enabled: bool
    woo_error: str | None = None
    in_error: str | None = None
    kpis: dict = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    invoices: list[dict] = field(default_factory=list)


_STATUS_LABEL = {
    "1": "Entwurf",
    "2": "Versendet",
    "3": "Teilzahlung",
    "4": "Bezahlt",
    "5": "Storniert",
}


def status_label(status_id) -> str:
    return _STATUS_LABEL.get(str(status_id), "—")


def build_hub_view(
    db: Session, include_drafts: bool = False, include_archived: bool = True,
    period: str = "all",
) -> HubView:
    view = HubView(woo_enabled=woo.is_enabled(), in_enabled=invoiceninja.is_enabled())

    # Map of woo_order_id -> OrderInvoice for linking.
    links = {
        li.woo_order_id: li
        for li in db.query(OrderInvoice).filter(OrderInvoice.woo_order_id.isnot(None))
    }

    if view.woo_enabled:
        try:
            for o in woo.list_orders():
                link = links.get(o.get("id"))
                view.orders.append(
                    {
                        "id": o.get("id"),
                        "number": o.get("number"),
                        "status": o.get("status"),
                        "total": float(o.get("total") or 0),
                        "currency": o.get("currency", ""),
                        "date": (o.get("date_created") or "")[:10],
                        "customer": (
                            f"{(o.get('billing') or {}).get('first_name','')} "
                            f"{(o.get('billing') or {}).get('last_name','')}"
                        ).strip(),
                        "email": (o.get("billing") or {}).get("email", ""),
                        "invoice": link,
                    }
                )
        except Exception as e:  # noqa: BLE001 - surface to UI, don't crash
            view.woo_error = str(e)

    if view.in_enabled:
        try:
            view.kpis = invoiceninja.get_company_totals(period)
            invs = invoiceninja.filter_period(
                invoiceninja.list_invoices(limit=80, include_archived=include_archived),
                period,
            )
            for inv in invs:
                # Drafts (status_id == 1) are hidden by default — these are
                # often leftover test invoices that clutter the overview.
                if not include_drafts and str(inv.get("status_id")) == "1":
                    continue
                client = inv.get("client") or {}
                balance = float(inv.get("balance") or 0)
                due = inv.get("due_date") or ""
                overdue = False
                if due and balance > 0:
                    try:
                        overdue = _date.fromisoformat(due) < _date.today()
                    except ValueError:
                        overdue = False
                view.invoices.append(
                    {
                        "id": inv.get("id"),
                        "number": inv.get("number"),
                        "amount": float(inv.get("amount") or 0),
                        "balance": balance,
                        "status_id": inv.get("status_id"),
                        "status": status_label(inv.get("status_id")),
                        "archived": bool(inv.get("archived_at")),
                        "date": inv.get("date", ""),
                        "due_date": due,
                        "overdue": overdue,
                        "client": client.get("display_name") or client.get("name") or "",
                    }
                )
        except Exception as e:  # noqa: BLE001
            view.in_error = str(e)

    return view
