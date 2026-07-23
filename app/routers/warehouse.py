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


def _set_members(
    part_name, part_sale, part_purchase, part_note, part_image, part_receipt=None
) -> list[dict]:
    """Parse the set-part sub-forms into member dicts (full products: name,
    sale value, own purchase price, note, image, optional own receipt), one per
    non-empty name. A part with its own receipt was bought separately and gets
    its own expense; the rest share the set-level receipt."""
    part_receipt = part_receipt or []
    members = []
    for i, nm in enumerate(part_name):
        if not nm.strip():
            continue
        img = None
        if i < len(part_image) and part_image[i] is not None and part_image[i].filename:
            img, _ = save_image_or_error(part_image[i], "part")
        rcpt = None
        if i < len(part_receipt) and part_receipt[i] is not None and part_receipt[i].filename:
            rcpt = save_receipt(part_receipt[i], "receipt")
        members.append({
            "name": nm.strip(),
            "sale": _parse_float(part_sale[i]) if i < len(part_sale) else None,
            "purchase": _parse_float(part_purchase[i]) if i < len(part_purchase) else None,
            "note": (part_note[i].strip() if i < len(part_note) else "") or None,
            "image_path": img,
            "receipt_path": rcpt,
        })
    return members


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


def _make_set(db, *, name, total, sale_price, image_path, rpath, members, src_expense_id=None) -> PartSet:
    """Create a PartSet + its member parts (full products; cost split
    value-weighted). If a receipt path is given, one warehouse expense is
    created for the whole set."""
    # Explicit per-part purchase prices win; parts left blank share the
    # remainder of the set total, value-weighted by their sale value.
    explicit_sum = sum(m["purchase"] for m in members if m.get("purchase") is not None)
    set_total = total if (total and total >= explicit_sum) else explicit_sum
    remainder = max(0.0, set_total - explicit_sum)
    blank = [m for m in members if m.get("purchase") is None]
    rem_alloc = _allocate(remainder, [(m["sale"] or 0.0) for m in blank])
    ai = 0
    for m in members:
        if m.get("purchase") is not None:
            m["_cost"] = m["purchase"]
        else:
            m["_cost"] = rem_alloc[ai] if ai < len(rem_alloc) else 0.0
            ai += 1

    # A part bought separately (its own receipt) gets its own expense; the rest
    # are the "lot" documented by the set-level receipt. Splitting the expense
    # this way keeps the books exact — no double counting.
    for m in members:
        if m.get("receipt_path"):
            e = exp_service.create(
                db, amount=m["_cost"], expense_date=date.today(), vendor="",
                description=f"Set part: {m['name']}", category="Parts",
                project_id=None, receipt_path=m["receipt_path"], bucket="warehouse",
            )
            m["_exp_id"] = e.id
        else:
            m["_exp_id"] = None

    lot_total = round(
        sum(m["_cost"] for m in members if not m.get("receipt_path")), 2
    )
    set_exp_id = src_expense_id
    if rpath and lot_total > 0:
        exp = exp_service.create(
            db, amount=lot_total, expense_date=date.today(), vendor="",
            description=f"Set: {name}", category="Parts",
            project_id=None, receipt_path=rpath, bucket="warehouse",
        )
        set_exp_id = exp.id
    ps = PartSet(
        name=name, purchase_price=set_total, sale_price=sale_price,
        image_path=image_path, expense_id=set_exp_id,
    )
    db.add(ps)
    db.flush()
    for m in members:
        db.add(Part(
            name=m["name"], notes=m["note"], project_id=None, device_id=None,
            set_id=ps.id, image_path=m["image_path"],
            origin=PartOrigin.purchased if m["_cost"] else PartOrigin.harvested,
            purchase_price=m["_cost"] or None, sale_price=m["sale"] or 0.0,
            source_expense_id=m["_exp_id"] or set_exp_id,
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
    stock_value = sum((p.sale_price or 0.0) * (p.quantity or 1) for p in parts)
    stock_cost = sum(
        (p.purchase_price or 0.0) * (p.quantity or 1)
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
    quantity: str = Form("1"),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    part_purchase: list[str] = Form(default=[]),
    part_note: list[str] = Form(default=[]),
    part_image: list[UploadFile] = File(default=[]),
    part_receipt: list[UploadFile] = File(default=[]),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create one warehouse part — or, if set-parts are supplied, a set: the top
    fields become the set (name + total price + optional own sale price +
    receipt) and the total is split across the set-parts (each a full product).
    A set-part may carry its own receipt (bought separately)."""
    members = _set_members(
        part_name, part_sale, part_purchase, part_note, part_image, part_receipt
    )
    is_free = free.strip().lower() in ("1", "on", "true", "yes")
    img_url, img_err = save_image_or_error(image, "part")

    if members:
        total = _parse_float(purchase_price) or 0.0
        # A set is documented if it's free, has a set-level receipt, or every
        # part brought its own receipt (assembled from separate purchases).
        rpath = None if is_free else save_receipt(receipt, "receipt")
        if not is_free and not rpath and not all(m["receipt_path"] for m in members):
            return RedirectResponse(
                "/warehouse?msg=Beleg erforderlich: ein Set-Beleg oder ein Beleg je Teil (oder 'gratis')",
                status_code=303,
            )
        _make_set(
            db, name=name.strip(), total=total,
            sale_price=_parse_float(sale_price), image_path=img_url,
            rpath=rpath, members=members,
        )
        return RedirectResponse(
            "/warehouse?msg=Set angelegt" + (f" — {img_err}" if img_err else ""),
            status_code=303,
        )

    # A single paid part must be documented with a receipt; only "free" is exempt.
    rpath = None
    if not is_free:
        rpath = save_receipt(receipt, "receipt")
        if not rpath:
            return RedirectResponse(
                "/warehouse?msg=Beleg erforderlich (oder als 'gratis' markieren)",
                status_code=303,
            )

    qty = max(1, int(_parse_float(quantity) or 1))
    # The form fields are the lot totals; prices are stored per unit so the
    # stock math (per-unit × quantity) reproduces the total.
    pp_total = None if is_free else _parse_float(purchase_price)
    sale_total = _parse_float(sale_price) or 0.0
    part = Part(
        name=name.strip(),
        notes=notes.strip() or None,
        project_id=None,
        origin=PartOrigin.purchased if pp_total else PartOrigin.harvested,
        purchase_price=(pp_total / qty) if pp_total else None,
        sale_price=sale_total / qty,
        image_path=img_url,
        quantity=qty,
    )
    db.add(part)
    db.commit()
    if rpath:
        exp = exp_service.create(
            db, amount=pp_total or 0.0, expense_date=date.today(), vendor="",
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
    part_purchase: list[str] = Form(default=[]),
    part_note: list[str] = Form(default=[]),
    part_image: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Turn an existing warehouse part into a set: its cost is split across the
    new set-parts (each a full product) and the original part is replaced."""
    part = db.get(Part, part_id)
    if not part or part.project_id is not None:
        return RedirectResponse("/warehouse", status_code=303)
    members = _set_members(part_name, part_sale, part_purchase, part_note, part_image)
    if not members:
        return RedirectResponse("/warehouse?msg=No parts given", status_code=303)
    total = _parse_float(total_price)
    if total is None:
        total = part.purchase_price or 0.0
    _make_set(
        db, name=part.name, total=total, sale_price=part.sale_price or None,
        image_path=part.image_path, rpath=None, members=members,
        src_expense_id=part.source_expense_id,
    )
    db.delete(part)
    db.commit()
    return RedirectResponse(
        f"/warehouse?msg=In Set mit {len(members)} Teilen aufgeteilt", status_code=303
    )


@router.post("/{part_id}/update")
async def update_part(
    part_id: int,
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    notes: str = Form(""),
    quantity: str = Form("1"),
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
        part.quantity = max(1, int(_parse_float(quantity) or 1))
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
