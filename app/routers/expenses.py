from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Expense, Project, ProjectStatus
from ..services import expenses as exp_service
from ..services.integrations import invoiceninja
from ..services.uploads import save_image, save_receipt_or_error
from ..templating import ctx, templates

router = APIRouter(prefix="/expenses")


def _parse_float(v: str | None) -> float:
    if not v or not v.strip():
        return 0.0
    try:
        return float(v.replace(",", ".").strip())
    except ValueError:
        return 0.0


def _parse_date(v: str) -> date:
    try:
        return date.fromisoformat(v) if v else date.today()
    except ValueError:
        return date.today()


def _parse_allocation(sel: str) -> tuple[int | None, str | None]:
    """The project select carries either a project id, or 'warehouse' /
    'advertisement'. Returns (project_id, bucket)."""
    sel = (sel or "").strip()
    if sel.isdigit():
        return int(sel), "project"
    if sel in ("warehouse", "advertisement"):
        return None, sel
    return None, None


@router.get("")
async def list_expenses(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    rows = db.query(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
    projects = (
        db.query(Project)
        .filter(Project.status != ProjectStatus.sold)
        .order_by(Project.name)
        .all()
    )
    total = sum((e.amount or 0.0) for e in rows)
    # Map each expense to its single linked warehouse product (if exactly one),
    # so the editor can pre-fill / update it instead of creating a duplicate.
    from collections import defaultdict

    from ..models import Part, PartSet

    by_exp: dict[int, list] = defaultdict(list)
    for p in (
        db.query(Part)
        .filter(Part.source_expense_id.isnot(None), Part.project_id.is_(None))
        .all()
    ):
        by_exp[p.source_expense_id].append(p)
    linked_parts = {eid: ps[0] for eid, ps in by_exp.items() if len(ps) == 1}

    # A receipt scan says little at thumbnail size, so an expense is shown with
    # the image of the product it paid for. Unlike `linked_parts` this covers
    # parts already installed into a project and expenses with several parts —
    # the oldest part wins, and a part beats a set booked on the same receipt.
    set_img: dict[int, str] = {}
    for ps_ in (
        db.query(PartSet)
        .filter(PartSet.expense_id.isnot(None), PartSet.image_path.isnot(None))
        .order_by(PartSet.id)
        .all()
    ):
        set_img.setdefault(ps_.expense_id, ps_.image_path)
    part_img: dict[int, str] = {}
    for p in (
        db.query(Part)
        .filter(Part.source_expense_id.isnot(None), Part.image_path.isnot(None))
        .order_by(Part.id)
        .all()
    ):
        part_img.setdefault(p.source_expense_id, p.image_path)
    linked_images = {**set_img, **part_img}

    return templates.TemplateResponse(
        "expenses/list.html",
        ctx(
            request, db, active="expenses",
            rows=rows, projects=projects, total=total,
            linked_parts=linked_parts, linked_images=linked_images,
            in_enabled=invoiceninja.is_enabled(),
            today=date.today().isoformat(),
        ),
    )


@router.post("")
async def create_expense(
    name: str = Form(""),
    amount: str = Form("0"),
    expense_date: str = Form(""),
    vendor: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    project_id: str = Form(""),
    receipt: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    product_name: str = Form(""),
    product_sale: str = Form(""),
    product_note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    receipt_path, err = save_receipt_or_error(receipt, "receipt")
    if err:
        return RedirectResponse(f"/expenses?msg={err}", status_code=303)
    if not receipt_path:
        return RedirectResponse(
            "/expenses?msg=A receipt (PDF/image) is required for every expense.",
            status_code=303,
        )
    pid, bucket = _parse_allocation(project_id)
    img_path = save_image(image, "expense")
    exp = exp_service.create(
        db, name=name.strip(), amount=_parse_float(amount),
        expense_date=_parse_date(expense_date),
        vendor=vendor.strip(), description=description.strip(), category=category.strip(),
        project_id=pid, bucket=bucket,
        receipt_path=receipt_path, image_path=img_path,
    )
    msg = "Expense saved"
    # Optionally spin up a warehouse product from this purchase, linked to the
    # expense (which documents the receipt); the amount is its purchase cost.
    if product_name.strip():
        from ..models import Part, PartOrigin

        cost = _parse_float(amount)
        db.add(Part(
            name=product_name.strip(), notes=product_note.strip() or None,
            project_id=None, device_id=None,
            origin=PartOrigin.purchased if cost else PartOrigin.harvested,
            purchase_price=cost or None, sale_price=_parse_float(product_sale) or 0.0,
            image_path=img_path, source_expense_id=exp.id,
        ))
        db.commit()
        msg = "Expense + product saved"
    return RedirectResponse(f"/expenses?msg={msg}", status_code=303)


@router.post("/{expense_id}/update")
async def update_expense(
    expense_id: int,
    name: str = Form(""),
    amount: str = Form("0"),
    expense_date: str = Form(""),
    vendor: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    project_id: str = Form(""),
    receipt: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    product_name: str = Form(""),
    product_sale: str = Form(""),
    product_note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    exp = db.get(Expense, expense_id)
    if not exp:
        return RedirectResponse("/expenses", status_code=303)
    receipt_path, err = save_receipt_or_error(receipt, "receipt")
    if err:
        return RedirectResponse(f"/expenses?msg={err}", status_code=303)
    pid, bucket = _parse_allocation(project_id)
    exp_service.update(
        db, exp, name=name.strip(), amount=_parse_float(amount),
        expense_date=_parse_date(expense_date),
        vendor=vendor.strip(), description=description.strip(), category=category.strip(),
        project_id=pid, bucket=bucket,
        receipt_path=receipt_path, image_path=save_image(image, "expense"),
    )
    msg = "Expense updated"
    # Link/refresh a warehouse product for this expense (upsert its single
    # linked part — create if none, update if exactly one, skip if it's a set).
    if product_name.strip():
        from ..models import Part, PartOrigin

        cost = _parse_float(amount)
        parts = (
            db.query(Part)
            .filter(Part.source_expense_id == exp.id, Part.project_id.is_(None))
            .all()
        )
        part = parts[0] if len(parts) == 1 else None
        if part is None:
            part = Part(project_id=None, device_id=None, source_expense_id=exp.id)
            db.add(part)
        part.name = product_name.strip()
        part.notes = product_note.strip() or None
        part.purchase_price = cost or None
        part.sale_price = _parse_float(product_sale) or 0.0
        part.origin = PartOrigin.purchased if cost else PartOrigin.harvested
        part.image_path = exp.image_path
        db.commit()
        msg = "Expense + product saved"
    return RedirectResponse(f"/expenses?msg={msg}", status_code=303)


@router.post("/resync")
async def resync_expenses(
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Re-align InvoiceNinja with the local expenses — for when they were
    wiped or changed on the IN side and the normal push skips them."""
    if not invoiceninja.is_enabled():
        return RedirectResponse("/expenses?msg=InvoiceNinja ist deaktiviert", status_code=303)
    created, updated, failed = exp_service.resync_all(db)
    msg = f"IN-Sync: {created} neu angelegt, {updated} aktualisiert"
    if failed:
        msg += f", {failed} fehlgeschlagen"
    return RedirectResponse(f"/expenses?msg={msg}", status_code=303)


@router.post("/{expense_id}/delete")
async def delete_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    exp = db.get(Expense, expense_id)
    if exp:
        exp_service.delete(db, exp)
    return RedirectResponse("/expenses?msg=Expense deleted", status_code=303)
