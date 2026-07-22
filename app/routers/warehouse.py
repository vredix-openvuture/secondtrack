from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Part, PartOrigin, PartSet, Project, ProjectStatus
from ..services import expenses as exp_service
from ..services.integrations import ebay
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


def _pairs_from_form(part_name: list[str], part_sale: list[str]) -> list[tuple[str, float | None]]:
    return [
        (part_name[i].strip(),
         _parse_float(part_sale[i]) if i < len(part_sale) else None)
        for i in range(len(part_name)) if part_name[i].strip()
    ]


def _allocate(total: float, sales: list[float]) -> list[float]:
    """Split `total` across items proportional to their sale value (equal split
    if no sale values); the last item absorbs rounding drift."""
    n = len(sales)
    if n == 0:
        return []
    ts = sum(sales)
    alloc = [round(total * s / ts, 2) for s in sales] if ts > 0 else [round(total / n, 2)] * n
    alloc[-1] = round(alloc[-1] + (total - sum(alloc)), 2)
    return alloc


def _make_set(db, *, name, total, sale_price, image_path, rpath, pairs, src_expense_id=None) -> PartSet:
    """Create a PartSet + its member parts (cost split value-weighted). If a
    receipt path is given, one warehouse expense is created for the whole set."""
    exp_id = src_expense_id
    if rpath:
        exp = exp_service.create(
            db, amount=total, expense_date=date.today(), vendor="",
            description=f"Set: {name}", category="Parts",
            project_id=None, receipt_path=rpath, bucket="warehouse",
        )
        exp_id = exp.id
    ps = PartSet(
        name=name, purchase_price=total, sale_price=sale_price,
        image_path=image_path, expense_id=exp_id,
    )
    db.add(ps)
    db.flush()
    for (pname, psale), cost in zip(pairs, _allocate(total, [s or 0.0 for _, s in pairs])):
        db.add(Part(
            name=pname, project_id=None, device_id=None, set_id=ps.id,
            origin=PartOrigin.purchased if cost else PartOrigin.harvested,
            purchase_price=cost or None, sale_price=psale or 0.0,
            source_expense_id=exp_id,
        ))
    db.commit()
    return ps


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
        .filter(Project.status.in_([ProjectStatus.open, ProjectStatus.in_progress]))
        .order_by(Project.name)
        .all()
    )
    stock_value = sum((p.sale_price or 0.0) for p in parts)
    stock_cost = sum(
        (p.purchase_price or 0.0)
        for p in parts
        if p.origin == PartOrigin.purchased
    )
    # Sets that still have at least one part in the warehouse.
    sets = [
        s for s in db.query(PartSet).order_by(PartSet.created_at.desc()).all()
        if any(p.project_id is None for p in s.parts)
    ]
    return templates.TemplateResponse(
        "warehouse/list.html",
        ctx(
            request,
            db,
            active="warehouse",
            parts=parts,
            sets=sets,
            projects=projects,
            stock_value=stock_value,
            stock_cost=stock_cost,
            ebay_enabled=ebay.is_enabled(),
        ),
    )


@router.get("/price-suggest")
async def price_suggest(
    q: str,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Rough market-price suggestion for a part name (eBay Browse API)."""
    if not ebay.is_enabled():
        return JSONResponse({"suggested": None, "count": 0, "error": "ebay disabled"})
    try:
        return JSONResponse(ebay.suggest_price(q.strip()))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"suggested": None, "count": 0, "error": str(e)[:200]})


@router.post("")
async def create_part(
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    notes: str = Form(""),
    free: str = Form(""),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create one warehouse part — or, if set-parts are supplied, a set: the top
    fields become the set (name + total price + optional own sale price +
    receipt) and the total is split across the set-parts."""
    set_pairs = _pairs_from_form(part_name, part_sale)
    is_free = free.strip().lower() in ("1", "on", "true", "yes")
    img_url, img_err = save_image_or_error(image, "part")

    # A paid purchase must be documented with a receipt; only "free/gift" is exempt.
    rpath = None
    if not is_free:
        rpath = save_receipt(receipt, "receipt")
        if not rpath:
            return RedirectResponse(
                "/warehouse?msg=Beleg erforderlich (oder als 'gratis' markieren)",
                status_code=303,
            )

    if set_pairs:
        total = _parse_float(purchase_price) or 0.0
        _make_set(
            db, name=name.strip(), total=total,
            sale_price=_parse_float(sale_price), image_path=img_url,
            rpath=rpath, pairs=set_pairs,
        )
        return RedirectResponse(
            "/warehouse?msg=Set angelegt" + (f" — {img_err}" if img_err else ""),
            status_code=303,
        )

    pp = None if is_free else _parse_float(purchase_price)
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
    if rpath:
        exp = exp_service.create(
            db, amount=pp or 0.0, expense_date=date.today(), vendor="",
            description=f"Part: {part.name}", category="Parts",
            project_id=None, receipt_path=rpath, bucket="warehouse",
        )
        part.source_expense_id = exp.id
        db.commit()
    return RedirectResponse(
        "/warehouse" + (f"?msg={img_err}" if img_err else ""), status_code=303
    )


@router.post("/{part_id}/split")
async def split_part(
    part_id: int,
    total_price: str = Form(""),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Turn an existing warehouse part into a set: its cost is split across the
    new set-parts and the original part is replaced by the set."""
    part = db.get(Part, part_id)
    if not part or part.project_id is not None:
        return RedirectResponse("/warehouse", status_code=303)
    pairs = _pairs_from_form(part_name, part_sale)
    if not pairs:
        return RedirectResponse("/warehouse?msg=No parts given", status_code=303)
    total = _parse_float(total_price)
    if total is None:
        total = part.purchase_price or 0.0
    _make_set(
        db, name=part.name, total=total, sale_price=part.sale_price or None,
        image_path=part.image_path, rpath=None, pairs=pairs,
        src_expense_id=part.source_expense_id,
    )
    db.delete(part)
    db.commit()
    return RedirectResponse(
        f"/warehouse?msg=In Set mit {len(pairs)} Teilen aufgeteilt", status_code=303
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
