"""Hub aggregation: ties WooCommerce orders, projects and InvoiceNinja
invoices into one coherent business overview, and orchestrates invoice
creation/sending. All invoice generation stays in InvoiceNinja."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime

from sqlalchemy.orm import Session

from .. import runtime
from ..models import (
    Customer,
    CustomerKind,
    InvoiceSource,
    OrderInvoice,
    Project,
    ProjectKind,
    ProjectStatus,
)
from .integrations import invoiceninja, vikunja, woo



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

    # Prefer the project's linked customer (its InvoiceNinja client), if any.
    if not client_id and project.customer_id:
        cust = db.get(Customer, project.customer_id)
        if cust and cust.invoiceninja_client_id:
            client_id = cust.invoiceninja_client_id

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


def _ensure_customer_for_order(
    db: Session, order: dict, in_client_id, link: OrderInvoice
) -> None:
    """Make the order's customer available locally as a secondtrack Customer,
    backed by its InvoiceNinja client. Idempotent: dedupe by IN client id, then
    email. Sets link.customer_id. Mirrors projects._resolve_customer."""
    if link.customer_id:
        return
    billing = order.get("billing", {}) or {}
    email = (billing.get("email") or "").strip()
    in_id = str(in_client_id) if in_client_id else ""

    cust = None
    if in_id:
        cust = (
            db.query(Customer)
            .filter(Customer.invoiceninja_client_id == in_id)
            .first()
        )
    if cust is None and email:
        cust = db.query(Customer).filter(Customer.email == email).first()

    if cust is None:
        name = " ".join(
            p for p in [billing.get("first_name", ""), billing.get("last_name", "")] if p
        ).strip() or (billing.get("company") or "").strip() or email or "Kunde"
        cust = Customer(
            name=name,
            kind=CustomerKind.invoiceninja if in_id else CustomerKind.internal,
            invoiceninja_client_id=in_id or None,
            email=email or None,
            company=(billing.get("company") or "").strip() or None,
        )
        db.add(cust)
        db.flush()  # obtain cust.id
    elif in_id and not cust.invoiceninja_client_id:
        cust.invoiceninja_client_id = in_id  # backfill now that we know it

    link.customer_id = cust.id
    db.commit()


def _order_task_text(order: dict, invoice_number: str = "") -> tuple[str, str]:
    """(title, HTML description) for a Woo order's fulfillment task — the packing
    list, shipping address, contact, total and links a human needs to ship it."""
    import html as _html

    def esc(s) -> str:
        return _html.escape(str(s or ""))

    billing = order.get("billing", {}) or {}
    shipping = order.get("shipping", {}) or {}
    number = order.get("number") or order.get("id") or ""
    name = " ".join(
        p for p in [
            (shipping.get("first_name") or billing.get("first_name") or ""),
            (shipping.get("last_name") or billing.get("last_name") or ""),
        ] if p
    ).strip() or (billing.get("company") or "").strip() or "Kunde"

    title = f"📦 Bestellung #{number} – {name}"

    items = []
    for li in order.get("line_items", []) or []:
        qty = li.get("quantity", 1) or 1
        line = f"{esc(li.get('name', 'Artikel'))} × {qty}"
        if li.get("sku"):
            line += f" (SKU {esc(li['sku'])})"
        items.append(f"<li>{line}</li>")
    items_html = "<ul>" + "".join(items) + "</ul>" if items else "<p>—</p>"

    # Prefer the shipping address; fall back to billing if none was given.
    addr = shipping if (shipping.get("address_1") or shipping.get("city")) else billing
    addr_lines = [
        name,
        addr.get("company", ""),
        addr.get("address_1", ""),
        addr.get("address_2", ""),
        " ".join(p for p in [addr.get("postcode", ""), addr.get("city", "")] if p),
        addr.get("country", ""),
    ]
    addr_html = "<br>".join(esc(x) for x in addr_lines if x)

    contact = []
    if billing.get("email"):
        contact.append(f"✉ {esc(billing['email'])}")
    if billing.get("phone"):
        contact.append(f"☎ {esc(billing['phone'])}")

    parts = [
        "<p><strong>Verpacken &amp; verschicken:</strong></p>",
        items_html,
        f"<p><strong>Lieferadresse</strong><br>{addr_html}</p>",
    ]
    if contact:
        parts.append(f"<p>{' · '.join(contact)}</p>")
    if order.get("total"):
        parts.append(
            f"<p><strong>Summe:</strong> {esc(order.get('total'))} "
            f"{esc(order.get('currency'))}</p>"
        )
    note = (order.get("customer_note") or "").strip()
    if note:
        parts.append(f"<p><strong>Kundennotiz:</strong> {esc(note)}</p>")
    if invoice_number:
        parts.append(f"<p>Rechnung: {esc(invoice_number)}</p>")

    woo_url = runtime.get("woo_url").rstrip("/")
    oid = order.get("id")
    if woo_url and oid:
        href = f"{woo_url}/wp-admin/post.php?post={oid}&action=edit"
        parts.append(f'<p><a href="{esc(href)}">Bestellung im Shop öffnen ↗</a></p>')

    return title, "".join(parts)


def _ensure_order_task(db: Session, order: dict, link: OrderInvoice) -> None:
    """Create a Vikunja fulfillment task for the order in the configured board
    (default 'customers'). Idempotent (skips if link.vikunja_task_id set). Never
    raises — a Vikunja hiccup must not break the receipt path."""
    if link.vikunja_task_id:
        return
    if not runtime.get_bool("woo_task_enabled") or not vikunja.is_enabled():
        return
    try:
        board = runtime.get("vikunja_order_board") or "customers"
        pid = vikunja.find_or_create_subproject(board)
        title, desc = _order_task_text(order, link.invoice_number or "")
        task = vikunja.create_task(pid, title, description=desc)
        link.vikunja_task_id = str(task.get("id") or "") or None
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def fulfill_order_as_receipt(db: Session, order_id: int) -> OrderInvoice:
    """Direct (paid) shop order → create the document in IN, mark it PAID
    (records income, becomes a receipt) and email the receipt to the customer.
    Also links the customer locally and creates a Vikunja fulfillment task."""
    import time

    from . import emails

    link = create_invoice_for_order(db, order_id, auto_send=False)

    # Fetch the order + its invoice once; reused for reconcile, customer, task, payment.
    try:
        order = woo.get_order(order_id)
    except Exception:  # noqa: BLE001
        order = {}
    inv = invoiceninja.get_invoice(link.invoiceninja_id) or {}
    client_id = inv.get("client_id")

    # Reconcile: if the ordered product was produced in-house (a shop project),
    # link the sale to that project and mark it sold.
    if not link.project_id and order:
        try:
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

    # Make the customer available locally and create the fulfillment task
    # ("what to pack & ship"). Both idempotent; neither may break the receipt path.
    if order:
        try:
            _ensure_customer_for_order(db, order, client_id, link)
        except Exception:  # noqa: BLE001
            db.rollback()
        _ensure_order_task(db, order, link)

    if link.emailed_at:  # already handled before
        return link

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
    <base>/Invoices/<year>/<month>/<number>_<customer>.pdf"""
    number = _safe_name(str(inv.get("number") or inv.get("id")))
    client = inv.get("client") or {}
    cname = _safe_name(client.get("display_name") or client.get("name") or "")
    d = invoiceninja._invoice_date(inv)
    if d:
        year, month = str(d.year), f"{d.month:02d}"
    else:
        now = datetime.utcnow()
        year, month = now.strftime("%Y"), now.strftime("%m")
    stem = f"{number}_{cname}".strip("_ ") or number
    base = (runtime.get("nc_base_path") or "/OpenVuture/Belege").rstrip("/")
    return f"{base}/Invoices/{year}/{month}/{stem}.pdf"


def _deleted_path(remote_path: str) -> str:
    """The same file relocated into a 'deleted/' subfolder of its own folder."""
    folder, _, filename = remote_path.rpartition("/")
    return f"{folder}/deleted/{filename}"


def _invoice_version(inv: dict) -> str:
    """A change-detection key for an invoice — bumps whenever InvoiceNinja
    touches it, so we know to re-upload the PDF to Nextcloud."""
    return str(inv.get("updated_at") or inv.get("amount") or "")


def _sync_index(db: Session) -> dict:
    """{id: {'ver': .., 'path': ..}} of invoices placed in Nextcloud. Migrates
    the older ids-CSV + versions-JSON settings on first read (paths unknown for
    those until the next sync re-uploads them)."""
    import json

    from ..db import get_setting

    try:
        idx = json.loads(get_setting(db, "nc_archive_index", "") or "{}")
    except (ValueError, TypeError):
        idx = {}
    if not idx:
        legacy = {
            x for x in (get_setting(db, "nc_archived_invoice_ids", "") or "").split(",") if x
        }
        try:
            vers = json.loads(get_setting(db, "nc_archived_versions", "") or "{}")
        except (ValueError, TypeError):
            vers = {}
        idx = {i: {"ver": vers.get(i, ""), "path": None} for i in legacy}
    return idx


def _save_sync_index(db: Session, idx: dict) -> None:
    import json

    from ..db import set_setting

    set_setting(db, "nc_archive_index", json.dumps(idx))
    # keep the flat id list in sync for the per-row "synced" indicator
    set_setting(db, "nc_archived_invoice_ids", ",".join(sorted(idx.keys())))


def archive_invoice(db: Session, link: OrderInvoice, kind: str = "") -> str:
    """Fetch the invoice PDF from InvoiceNinja and store it in Nextcloud under
    Invoices/<year>/<month>/<number>_<customer>.pdf. Returns the remote path."""
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
    path = _invoice_remote_path(inv)
    remote = nextcloud.put_file(path, pdf, "application/pdf")
    # Record sync state so the row indicator turns green and re-syncs track edits.
    idx = _sync_index(db)
    idx[str(link.invoiceninja_id)] = {"ver": _invoice_version(inv), "path": path}
    _save_sync_index(db, idx)
    return remote


def archive_paid_invoices(db: Session) -> dict:
    """Sync PAID InvoiceNinja invoices into Nextcloud
    (Invoices/<year>/<month>/<number>_<customer>.pdf): upload once, re-upload
    (overwrite) on change, and move a deleted invoice's PDF into a 'deleted/'
    subfolder of its month. Returns {'archived': n, 'updated': m, 'deleted': k}."""
    from .integrations import nextcloud

    if not nextcloud.is_enabled() or not invoiceninja.is_enabled():
        return {"archived": 0, "updated": 0, "deleted": 0, "skipped": "integration disabled"}

    idx = _sync_index(db)
    archived = updated = deleted = 0
    # include_deleted so we can relocate PDFs of invoices removed in IN
    for inv in invoiceninja.list_invoices(limit=400, include_archived=True, include_deleted=True):
        iid = str(inv.get("id"))
        if inv.get("is_deleted"):
            entry = idx.get(iid)
            if entry and entry.get("path"):
                try:
                    if nextcloud.move_file(entry["path"], _deleted_path(entry["path"])):
                        deleted += 1
                except Exception:  # noqa: BLE001
                    pass
            idx.pop(iid, None)  # no longer counts as synced (moved to deleted/)
            continue
        if str(inv.get("status_id")) != "4":  # 4 = paid
            continue
        ver = _invoice_version(inv)
        entry = idx.get(iid)
        if entry and entry.get("ver") == ver and entry.get("path"):
            continue  # already synced and unchanged
        pdf = invoiceninja.download_pdf(iid)
        if not pdf:
            continue
        path = _invoice_remote_path(inv)
        try:
            nextcloud.put_file(path, pdf, "application/pdf")  # PUT overwrites
        except Exception:  # noqa: BLE001
            continue
        if entry:
            updated += 1
        else:
            archived += 1
        idx[iid] = {"ver": ver, "path": path}
    _save_sync_index(db, idx)
    return {"archived": archived, "updated": updated, "deleted": deleted}


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
                        "task_id": link.vikunja_task_id if link else None,
                    }
                )
        except Exception as e:  # noqa: BLE001 - surface to UI, don't crash
            view.woo_error = str(e)

    if view.in_enabled:
        try:
            view.kpis = invoiceninja.get_company_totals(period)
            # Invoice ids we've already uploaded to Nextcloud (see
            # archive_paid_invoices) — used to show a per-row sync indicator.
            nc_synced = {
                x for x in (get_setting(db, "nc_archived_invoice_ids", "") or "").split(",") if x
            }
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
                        "nc_synced": str(inv.get("id")) in nc_synced,
                        "date": inv.get("date", ""),
                        "due_date": due,
                        "overdue": overdue,
                        "client": client.get("display_name") or client.get("name") or "",
                    }
                )
        except Exception as e:  # noqa: BLE001
            view.in_error = str(e)

    return view
