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
    return templates.TemplateResponse(
        "expenses/list.html",
        ctx(
            request, db, active="expenses",
            rows=rows, projects=projects, total=total,
            in_enabled=invoiceninja.is_enabled(),
            today=date.today().isoformat(),
        ),
    )


@router.post("")
async def create_expense(
    amount: str = Form("0"),
    expense_date: str = Form(""),
    vendor: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    project_id: str = Form(""),
    receipt: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
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
    pid = int(project_id) if project_id.strip().isdigit() else None
    exp_service.create(
        db, amount=_parse_float(amount), expense_date=_parse_date(expense_date),
        vendor=vendor.strip(), description=description.strip(), category=category.strip(),
        project_id=pid, receipt_path=receipt_path, image_path=save_image(image, "expense"),
    )
    return RedirectResponse("/expenses?msg=Expense saved", status_code=303)


@router.post("/{expense_id}/update")
async def update_expense(
    expense_id: int,
    amount: str = Form("0"),
    expense_date: str = Form(""),
    vendor: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    project_id: str = Form(""),
    receipt: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    exp = db.get(Expense, expense_id)
    if not exp:
        return RedirectResponse("/expenses", status_code=303)
    receipt_path, err = save_receipt_or_error(receipt, "receipt")
    if err:
        return RedirectResponse(f"/expenses?msg={err}", status_code=303)
    pid = int(project_id) if project_id.strip().isdigit() else None
    exp_service.update(
        db, exp, amount=_parse_float(amount), expense_date=_parse_date(expense_date),
        vendor=vendor.strip(), description=description.strip(), category=category.strip(),
        project_id=pid, receipt_path=receipt_path, image_path=save_image(image, "expense"),
    )
    return RedirectResponse("/expenses?msg=Expense updated", status_code=303)


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
