from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Part, PartOrigin, Project, ProjectStatus
from ..services import expenses as exp_service
from ..services.uploads import delete_image, save_image_or_error, save_receipt
from ..templating import ctx, templates

router = APIRouter(prefix="/warehouse")


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None


@router.get("")
async def warehouse_list(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    parts = (
        db.query(Part)
        .filter(Part.project_id.is_(None))
        .order_by(Part.created_at.desc())
        .all()
    )
    # Active projects to offer "install into…".
    projects = (
        db.query(Project)
        .filter(Project.status == ProjectStatus.in_production)
        .order_by(Project.name)
        .all()
    )
    stock_value = sum((p.sale_price or 0.0) for p in parts)
    stock_cost = sum(
        (p.purchase_price or 0.0)
        for p in parts
        if p.origin == PartOrigin.purchased
    )
    return templates.TemplateResponse(
        "warehouse/list.html",
        ctx(
            request,
            db,
            active="warehouse",
            parts=parts,
            projects=projects,
            stock_value=stock_value,
            stock_cost=stock_cost,
        ),
    )


@router.post("")
async def create_part(
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    pp = _parse_float(purchase_price)
    img_url, img_err = save_image_or_error(image, "part")
    part = Part(
        name=name.strip(),
        notes=notes.strip() or None,
        project_id=None,
        origin=PartOrigin.purchased if pp else PartOrigin.harvested,
        purchase_price=pp,
        sale_price=_parse_float(sale_price) or 0.0,
        image_path=img_url,
    )
    db.add(part)
    db.commit()
    rpath = save_receipt(receipt, "receipt")
    if rpath and pp and pp > 0:
        exp_service.create(
            db, amount=pp, expense_date=date.today(), vendor="",
            description=f"Part: {part.name}", category="Parts",
            project_id=None, receipt_path=rpath,
        )
    return RedirectResponse(
        "/warehouse" + (f"?msg={img_err}" if img_err else ""), status_code=303
    )


@router.post("/{part_id}/update")
async def update_part(
    part_id: int,
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    img_err = None
    if part and part.project_id is None:
        new_image, img_err = save_image_or_error(image, "part")
        if new_image:
            delete_image(part.image_path)
            part.image_path = new_image
        part.name = name.strip()
        part.notes = notes.strip() or None
        part.purchase_price = _parse_float(purchase_price)
        part.sale_price = _parse_float(sale_price) or 0.0
        part.origin = (
            PartOrigin.purchased if part.purchase_price else PartOrigin.harvested
        )
        db.commit()
    return RedirectResponse(
        "/warehouse" + (f"?msg={img_err}" if img_err else ""), status_code=303
    )


@router.post("/{part_id}/install")
async def install_part(
    part_id: int,
    project_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    project = db.get(Project, project_id)
    if part and part.project_id is None and project:
        part.project_id = project.id  # sale price carries over automatically
        db.commit()
        return RedirectResponse(f"/projects/{project.id}", status_code=303)
    return RedirectResponse("/warehouse", status_code=303)


@router.post("/{part_id}/delete")
async def delete_part(
    part_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    if part and part.project_id is None:
        db.delete(part)
        db.commit()
    return RedirectResponse("/warehouse", status_code=303)
