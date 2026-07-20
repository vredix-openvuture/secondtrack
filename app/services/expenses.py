"""Business expense tracking: stored locally with a receipt and mirrored to
InvoiceNinja expenses (with the receipt attached as a document)."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from ..models import Expense
from .integrations import invoiceninja
from .uploads import delete_image, read_upload


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
) -> Expense:
    exp = Expense(
        amount=amount, expense_date=expense_date,
        vendor=vendor or None, description=description or None,
        category=category or None, project_id=project_id,
        receipt_path=receipt_path, image_path=image_path,
    )
    db.add(exp)
    db.commit()
    push_to_in(db, exp)
    return exp


def update(
    db: Session, exp: Expense, *, amount: float, expense_date: date, vendor: str = "",
    description: str = "", category: str = "", project_id: int | None = None,
    receipt_path: str | None = None, image_path: str | None = None,
) -> Expense:
    exp.amount = amount
    exp.expense_date = expense_date
    exp.vendor = vendor or None
    exp.description = description or None
    exp.category = category or None
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
