"""Business expense tracking: stored locally with a receipt and mirrored to
InvoiceNinja expenses (with the receipt attached as a document)."""
from __future__ import annotations

import calendar
from datetime import date

from sqlalchemy.orm import Session

from ..models import Expense
from .integrations import invoiceninja
from .uploads import delete_image, read_upload

_MIME = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


def _safe(s: str) -> str:
    s = (s or "").strip()
    for ch in '/\\:*?"<>|':
        s = s.replace(ch, "-")
    return " ".join(s.split())


def archive_receipt_to_nextcloud(exp: Expense) -> None:
    """Store the receipt in Nextcloud at
    Expenses/<year>/<MM - Month>/<ISOdate>_<name>.<ext>. Never raises."""
    from .. import runtime
    from .integrations import nextcloud

    if not nextcloud.is_enabled() or not exp.receipt_path:
        return
    r = read_upload(exp.receipt_path)
    if not r:
        return
    fname, data = r[0], r[1]
    ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
    d = exp.expense_date
    label = _safe(exp.name or exp.vendor or "expense")
    month = f"{d.month:02d} - {calendar.month_name[d.month]}"
    base = (runtime.get("nc_base_path") or "/OpenVuture").rstrip("/")
    remote = f"{base}/Expenses/{d.year}/{month}/{d.isoformat()}_{label}{ext}"
    try:
        nextcloud.put_file(remote, data, _MIME.get(ext, "application/octet-stream"))
    except Exception:  # noqa: BLE001
        pass


def push_to_in(db: Session, exp: Expense) -> None:
    """Best-effort: create the expense in InvoiceNinja and attach the receipt."""
    if not invoiceninja.is_enabled() or exp.invoiceninja_id:
        return
    notes = exp.description or ""
    if exp.project and exp.project.name:
        notes = (notes + f"\nProject: {exp.project.name}").strip()
    try:
        data = invoiceninja.create_expense(
            amount=exp.amount, date_iso=exp.expense_date.isoformat(),
            notes=notes, category=exp.category or "", vendor=exp.vendor or "",
        )
        exp.invoiceninja_id = str(data.get("id") or "") or None
        db.commit()
        if exp.invoiceninja_id and exp.receipt_path:
            r = read_upload(exp.receipt_path)
            if r:
                invoiceninja.upload_expense_document(exp.invoiceninja_id, r[0], r[1])
    except Exception:  # noqa: BLE001 - keep the local record even if IN fails
        pass


def create(
    db: Session, *, amount: float, expense_date: date, vendor: str = "",
    description: str = "", category: str = "", project_id: int | None = None,
    receipt_path: str | None = None, image_path: str | None = None,
    name: str = "", bucket: str | None = None,
) -> Expense:
    exp = Expense(
        name=name or None, amount=amount, expense_date=expense_date,
        vendor=vendor or None, description=description or None,
        category=category or None, bucket=bucket, project_id=project_id,
        receipt_path=receipt_path, image_path=image_path,
    )
    db.add(exp)
    db.commit()
    archive_receipt_to_nextcloud(exp)
    push_to_in(db, exp)
    return exp


def update(
    db: Session, exp: Expense, *, amount: float, expense_date: date, vendor: str = "",
    description: str = "", category: str = "", project_id: int | None = None,
    receipt_path: str | None = None, image_path: str | None = None,
    name: str = "", bucket: str | None = None,
) -> Expense:
    exp.name = name or None
    exp.amount = amount
    exp.expense_date = expense_date
    exp.vendor = vendor or None
    exp.description = description or None
    exp.category = category or None
    exp.bucket = bucket
    exp.project_id = project_id
    new_receipt = False
    if receipt_path:
        delete_image(exp.receipt_path)
        exp.receipt_path = receipt_path
        new_receipt = True
    if image_path:
        delete_image(exp.image_path)
        exp.image_path = image_path
    db.commit()
    if new_receipt:
        archive_receipt_to_nextcloud(exp)

    # Sync the change to InvoiceNinja.
    if invoiceninja.is_enabled():
        if exp.invoiceninja_id:
            notes = exp.description or ""
            if exp.project and exp.project.name:
                notes = (notes + f"\nProject: {exp.project.name}").strip()
            try:
                invoiceninja.update_expense(
                    exp.invoiceninja_id, exp.amount, exp.expense_date.isoformat(),
                    notes, exp.category or "", exp.vendor or "",
                )
                if new_receipt and exp.receipt_path:
                    r = read_upload(exp.receipt_path)
                    if r:
                        invoiceninja.upload_expense_document(exp.invoiceninja_id, r[0], r[1])
            except Exception:  # noqa: BLE001
                pass
        else:
            push_to_in(db, exp)  # wasn't synced yet (e.g. IN was off at creation)
    return exp


def delete(db: Session, exp: Expense) -> None:
    if exp.invoiceninja_id and invoiceninja.is_enabled():
        try:
            invoiceninja.delete_expense(exp.invoiceninja_id)
        except Exception:  # noqa: BLE001
            pass
    delete_image(exp.receipt_path)
    delete_image(exp.image_path)
    db.delete(exp)
    db.commit()


def expenses_total(db: Session, start: date | None = None, end: date | None = None) -> float:
    q = db.query(Expense)
    if start:
        q = q.filter(Expense.expense_date >= start)
    if end:
        q = q.filter(Expense.expense_date <= end)
    return sum((e.amount or 0.0) for e in q.all())


def income_total(start: date | None = None, end: date | None = None) -> float:
    """Real income = paid amounts of InvoiceNinja invoices in the period."""
    if not invoiceninja.is_enabled():
        return 0.0
    try:
        invoices = invoiceninja.list_invoices(limit=400, include_archived=True)
    except Exception:  # noqa: BLE001
        return 0.0
    total = 0.0
    for inv in invoices:
        d = inv.get("date") or ""
        try:
            id_ = date.fromisoformat(d) if d else None
        except ValueError:
            id_ = None
        if start and (id_ is None or id_ < start):
            continue
        if end and (id_ is None or id_ > end):
            continue
        total += float(inv.get("amount") or 0) - float(inv.get("balance") or 0)
    return round(total, 2)


def profit_loss(db: Session, start: date | None = None, end: date | None = None) -> dict:
    income = income_total(start, end)
    expenses = expenses_total(db, start, end)
    return {
        "income": income,
        "expenses": round(expenses, 2),
        "profit": round(income - expenses, 2),
    }


def resync_all(db: Session) -> tuple[int, int, int]:
    """Bring InvoiceNinja back in line with the local expenses.

    Exists for the case where expenses were wiped or edited on the IN side:
    the local rows still carry their invoiceninja_id, so the normal push
    skips them as already-synced. Checks each id against IN — gone there
    means recreate, present means update to the local values.

    Returns (created, updated, failed).
    """
    if not invoiceninja.is_enabled():
        return 0, 0, 0
    created = updated = failed = 0
    for exp in db.query(Expense).order_by(Expense.expense_date, Expense.id).all():
        try:
            if exp.invoiceninja_id and invoiceninja.get_expense(exp.invoiceninja_id) is None:
                exp.invoiceninja_id = None
                db.commit()
            if exp.invoiceninja_id:
                notes = exp.description or ""
                if exp.project and exp.project.name:
                    notes = (notes + f"\nProject: {exp.project.name}").strip()
                invoiceninja.update_expense(
                    exp.invoiceninja_id, exp.amount, exp.expense_date.isoformat(),
                    notes, exp.category or "", exp.vendor or "",
                )
                updated += 1
            else:
                push_to_in(db, exp)
                created += 1 if exp.invoiceninja_id else 0
                failed += 0 if exp.invoiceninja_id else 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            failed += 1
    return created, updated, failed
