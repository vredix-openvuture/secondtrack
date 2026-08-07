from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import (
    Category,
    Expense,
    Part,
    PartOrigin,
    PartSet,
    Project,
    ProjectStatus,
    SetKind,
    StorageLocation,
    Supplier,
)
from ..services import codes
from ..services import expenses as exp_service
from ..services import warehouse as wh
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


def _parse_int(value: str | None) -> int | None:
    f = _parse_float(value)
    return int(f) if f is not None else None


def _parse_date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _fk(value: str | None) -> int | None:
    """Parse an optional foreign-key form value ('' → None)."""
    return _parse_int(value)


def _set_members(
    part_name, part_sale, part_purchase, part_note, part_image, part_receipt=None
) -> list[dict]:
    """Parse the set-part sub-forms into member dicts (full products: name,
    sale value, own purchase price, note, image, optional own receipt), one per
    non-empty name."""
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


def _make_set(
    db, *, name, total, sale_price, image_path, rpath, members,
    src_expense_id=None, supplier_id=None, location_id=None,
) -> PartSet:
    """Create a PartSet + its member parts (full products; cost split
    value-weighted). If a receipt path is given, one warehouse expense is
    created for the whole set. Every created object gets a scan code."""
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
        location_id=location_id, code=codes.generate(db, "set"),
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
            supplier_id=supplier_id, location_id=location_id,
            code=codes.generate(db, "part"),
        ))
    db.commit()
    return ps


def _form_lists(db):
    """Categories/suppliers/locations offered in the create & edit forms."""
    categories = db.query(Category).order_by(Category.position, Category.name).all()
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    locations = db.query(StorageLocation).order_by(StorageLocation.name).all()
    return categories, suppliers, locations


def _linkable_expenses(db, limit: int = 100):
    """Existing receipts offered in the create form, newest first. Only
    expenses that actually carry a receipt file can stand in for an upload."""
    return (
        db.query(Expense)
        .filter(Expense.receipt_path.isnot(None))
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .limit(limit)
        .all()
    )


def _recompute_assembly_cost(db, ps: PartSet) -> None:
    """An assembly's cost is the sum of its booked component parts (× quantity).
    Queries the DB directly (the ORM collection can be stale)."""
    if ps.kind == SetKind.assembly.value:
        members = db.query(Part).filter(Part.set_id == ps.id).all()
        ps.purchase_price = round(
            sum((p.purchase_price or 0.0) * (p.quantity or 1) for p in members), 2
        )


def _set_payload(db, ps: PartSet) -> dict:
    """Full set/finished-good state for the editor, incl. booked components
    and the loose parts still available to book."""
    member_rows = (
        db.query(Part).filter(Part.set_id == ps.id).order_by(Part.name).all()
    )
    members = [
        {
            "id": p.id, "name": p.name, "purchase": p.purchase_price,
            "sale": p.sale_price, "qty": p.quantity or 1,
        }
        for p in member_rows
    ]
    loose = (
        db.query(Part)
        .filter(
            Part.project_id.is_(None),
            Part.device_id.is_(None),
            Part.set_id.is_(None),
        )
        .order_by(Part.name)
        .all()
    )
    return {
        "id": ps.id,
        "name": ps.name,
        "sale_price": ps.sale_price,
        "purchase_price": ps.purchase_price,
        "location_id": ps.location_id or "",
        "sellable": ps.sellable,
        "notes": ps.notes or "",
        "code": ps.code or "",
        "image": ps.image_path or "",
        "is_assembly": ps.is_assembly,
        "is_wip": ps.is_wip,
        "status": ("wip" if ps.is_wip else "finished") if ps.is_assembly else "",
        "members": members,
        "vk_total": round(sum((m["sale"] or 0.0) * (m["qty"] or 1) for m in members), 2),
        "available": [{"id": p.id, "name": p.name, "qty": p.quantity or 1} for p in loose],
    }


@router.get("")
async def warehouse_list(
    request: Request,
    cat: str = "",
    low: str = "",
    view: str = "parts",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    view = view if view in ("parts", "sets", "wip", "finished") else "parts"
    # All warehouse parts (project & device NULL) — the basis for accounting and
    # set membership; the display list `parts` is this, optionally filtered.
    all_wh_parts = (
        db.query(Part)
        .filter(Part.project_id.is_(None), Part.device_id.is_(None))
        .order_by(Part.created_at.desc())
        .all()
    )
    active_cat = _fk(cat)
    only_low = low.strip() in ("1", "true", "yes")
    parts = all_wh_parts
    if active_cat:
        parts = [p for p in parts if p.category_id == active_cat]
    if only_low:
        parts = [p for p in parts if p.low_stock]

    projects = (
        db.query(Project)
        .filter(Project.status.in_([ProjectStatus.open, ProjectStatus.in_progress]))
        .order_by(Project.name)
        .all()
    )

    # Departments:
    #   single_parts — available parts: loose parts + members of purchase-lot
    #                  sets (a set part still lies in the workshop, so it stays
    #                  available). Only finished-good (assembly) members are
    #                  consumed and therefore hidden here.
    #   lots         — purchase-lot sets (one invoice/EK; parts keep own VK)
    #   finished     — assemblies / finished goods (built from parts, consumed)
    all_sets = db.query(PartSet).order_by(PartSet.created_at.desc()).all()
    lots = [s for s in all_sets if s.kind == SetKind.purchase_lot.value]
    assemblies = [s for s in all_sets if s.kind == SetKind.assembly.value]
    wip = [s for s in assemblies if s.status == "wip"]
    finished = [s for s in assemblies if s.status != "wip"]
    lot_ids = {s.id for s in lots}
    assembly_ids = {s.id for s in assemblies}  # wip + finished members are consumed

    single_parts = [p for p in parts if p.set_id is None or p.set_id in lot_ids]

    # Accounting (over ALL warehouse parts, unaffected by the view filter).
    #   EK: loose bought parts + each lot's total EK + each assembly's cost
    #       (WIP + finished both tie up material).
    #   VK: available parts' sale value + finished goods' price (WIP not sellable).
    stock_cost = sum(
        (p.purchase_price or 0.0) * (p.quantity or 1)
        for p in all_wh_parts
        if p.set_id is None and p.origin == PartOrigin.purchased
    ) + sum((s.purchase_price or 0.0) for s in lots) \
      + sum((s.purchase_price or 0.0) for s in assemblies)
    stock_value = sum(
        (p.sale_price or 0.0) * (p.quantity or 1)
        for p in all_wh_parts
        if p.set_id not in assembly_ids
    ) + sum((s.sale_price or 0.0) for s in finished)

    categories, suppliers, locations = _form_lists(db)
    low_stock = sum(1 for p in all_wh_parts if p.low_stock)
    single_count = sum(
        1 for p in all_wh_parts if p.set_id is None or p.set_id in lot_ids
    )
    # A part-specific filter (category/low) hides the set-based departments.
    filtering = bool(active_cat or only_low)
    return templates.TemplateResponse(
        "warehouse/list.html",
        ctx(
            request, db, active="warehouse",
            view=view, single_count=single_count,
            single_parts=single_parts, finished=finished, lots=lots, wip=wip,
            projects=projects,
            stock_value=stock_value, stock_cost=stock_cost, low_stock=low_stock,
            categories=categories, suppliers=suppliers, locations=locations,
            linkable_expenses=_linkable_expenses(db),
            optional_fields=wh.optional_fields(db),
            active_cat=active_cat, only_low=only_low, filtering=filtering, wh=wh,
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


@router.get("/{part_id}/json")
async def part_json(
    part_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Part data for the edit modal (mirrors the create form)."""
    p = db.get(Part, part_id)
    if not p or p.project_id is not None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "id": p.id,
        "name": p.name,
        "purchase_price": p.purchase_price,
        "sale_price": p.sale_price,
        "quantity": p.quantity or 1,
        "notes": p.notes or "",
        "category_id": p.category_id or "",
        "supplier_id": p.supplier_id or "",
        "location_id": p.location_id or "",
        "condition": p.condition or "",
        "code": p.code or "",
        "image": p.image_path or "",
        "attributes": p.attrs,
        "extra": p.extras,
    })


@router.post("")
async def create_part(
    request: Request,
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    notes: str = Form(""),
    free: str = Form(""),
    quantity: str = Form("1"),
    category_id: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    condition: str = Form(""),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    part_purchase: list[str] = Form(default=[]),
    part_note: list[str] = Form(default=[]),
    part_image: list[UploadFile] = File(default=[]),
    part_receipt: list[UploadFile] = File(default=[]),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    expense_id: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create one warehouse part — or, if set-parts are supplied, a set."""
    members = _set_members(
        part_name, part_sale, part_purchase, part_note, part_image, part_receipt
    )
    is_free = free.strip().lower() in ("1", "on", "true", "yes")
    img_url, img_err = save_image_or_error(image, "part")
    sup_id, loc_id = _fk(supplier_id), _fk(location_id)
    # An existing receipt can stand in for an upload: link it instead of
    # creating a second expense for a purchase that is already booked.
    linked_exp = db.get(Expense, _fk(expense_id)) if _fk(expense_id) else None

    if members:
        total = _parse_float(purchase_price) or 0.0
        rpath = None if (is_free or linked_exp) else save_receipt(receipt, "receipt")
        if (
            not is_free
            and not linked_exp
            and not rpath
            and not all(m["receipt_path"] for m in members)
        ):
            return RedirectResponse(
                "/warehouse?msg=Beleg erforderlich: ein Set-Beleg oder ein Beleg je Teil (oder 'gratis')",
                status_code=303,
            )
        _make_set(
            db, name=name.strip(), total=total,
            sale_price=_parse_float(sale_price), image_path=img_url,
            rpath=rpath, members=members, supplier_id=sup_id, location_id=loc_id,
            src_expense_id=linked_exp.id if linked_exp else None,
        )
        return RedirectResponse(
            "/warehouse?msg=Set angelegt" + (f" — {img_err}" if img_err else ""),
            status_code=303,
        )

    rpath = None
    if not is_free and not linked_exp:
        rpath = save_receipt(receipt, "receipt")
        if not rpath:
            return RedirectResponse(
                "/warehouse?msg=Beleg erforderlich (oder als 'gratis' markieren)",
                status_code=303,
            )

    qty = max(1, int(_parse_float(quantity) or 1))
    pp_total = None if is_free else _parse_float(purchase_price)
    sale_total = _parse_float(sale_price) or 0.0

    cat_id = _fk(category_id)
    category = db.get(Category, cat_id) if cat_id else None
    form = await request.form()
    attributes = wh.extract_attributes(category, form)
    extra = wh.extract_extra(wh.optional_fields(db), form)

    part = Part(
        name=name.strip(),
        notes=notes.strip() or None,
        project_id=None,
        origin=PartOrigin.purchased if pp_total else PartOrigin.harvested,
        purchase_price=(pp_total / qty) if pp_total else None,
        sale_price=sale_total / qty,
        image_path=img_url,
        quantity=qty,
        category_id=cat_id,
        supplier_id=sup_id,
        location_id=loc_id,
        attributes=attributes,
        extra=extra,
        condition=condition.strip() or None,
        code=codes.generate(db, codes.part_prefix(category)),
    )
    db.add(part)
    db.commit()
    if linked_exp:
        part.source_expense_id = linked_exp.id
        db.commit()
    elif rpath:
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
    """Turn an existing warehouse part into a set."""
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
        supplier_id=part.supplier_id, location_id=part.location_id,
    )
    db.delete(part)
    db.commit()
    return RedirectResponse(
        f"/warehouse?msg=In Set mit {len(members)} Teilen aufgeteilt", status_code=303
    )


@router.post("/{part_id}/update")
async def update_part(
    request: Request,
    part_id: int,
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    notes: str = Form(""),
    quantity: str = Form("1"),
    category_id: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    condition: str = Form(""),
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
        cat_id = _fk(category_id)
        part.category_id = cat_id
        category = db.get(Category, cat_id) if cat_id else None
        form = await request.form()
        part.attributes = wh.extract_attributes(category, form)
        part.extra = wh.extract_extra(wh.optional_fields(db), form)
        part.supplier_id = _fk(supplier_id)
        part.location_id = _fk(location_id)
        part.condition = condition.strip() or None
        if not part.code:
            part.code = codes.generate(db, codes.part_prefix(category))
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


@router.post("/{part_id}/move")
async def move_part(
    part_id: int,
    location_id: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Relocate a warehouse part to another storage location."""
    part = db.get(Part, part_id)
    if part and part.project_id is None:
        part.location_id = _fk(location_id)
        db.commit()
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


# ---- Sets / finished goods (W5) ----

@router.post("/set/{set_id}/update")
async def update_set(
    set_id: int,
    name: str = Form(...),
    sale_price: str = Form(""),
    sellable: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    ps = db.get(PartSet, set_id)
    img_err = None
    if ps:
        new_image, img_err = save_image_or_error(image, "set")
        if new_image:
            delete_image(ps.image_path)
            ps.image_path = new_image
        ps.name = name.strip()
        ps.sale_price = _parse_float(sale_price)
        ps.sellable = sellable.strip().lower() in ("1", "on", "true", "yes")
        ps.location_id = _fk(location_id)
        ps.notes = notes.strip() or None
        if not ps.code:
            ps.code = codes.generate(db, "finished")
        db.commit()
    return RedirectResponse(
        "/warehouse" + (f"?msg={img_err}" if img_err else "?msg=Saved"), status_code=303
    )


@router.get("/set/{set_id}/json")
async def set_json(
    set_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Set/finished-good data for the edit modal (incl. components)."""
    s = db.get(PartSet, set_id)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_set_payload(db, s))


@router.post("/finished")
async def create_finished(
    name: str = Form(...),
    sale_price: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create an empty finished good (assembly) to build up from booked parts."""
    ps = PartSet(
        name=name.strip() or "Finished good",
        kind=SetKind.assembly.value,
        status="finished",
        sellable=True,
        sale_price=_parse_float(sale_price),
        purchase_price=0.0,
        code=codes.generate(db, "finished"),
    )
    db.add(ps)
    db.commit()
    return RedirectResponse(f"/warehouse?view=finished&focus={ps.code}", status_code=303)


@router.post("/wip")
async def create_wip(
    name: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create a WIP assembly (in progress). Parts are booked onto it; when done
    it is moved to finished and gets a fresh finished storage number (PRD-…)."""
    ps = PartSet(
        name=name.strip() or "WIP",
        kind=SetKind.assembly.value,
        status="wip",
        sellable=False,
        purchase_price=0.0,
        code=codes.generate(db, "wip"),
    )
    db.add(ps)
    db.commit()
    return RedirectResponse(f"/warehouse?view=wip&focus={ps.code}", status_code=303)


@router.post("/set/{set_id}/finish")
async def finish_wip(
    set_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Move a WIP assembly to finished — assigns a fresh finished storage
    number (PRD-…), clearly identifying it as a finished part."""
    ps = db.get(PartSet, set_id)
    if ps and ps.is_assembly:
        ps.status = "finished"
        ps.sellable = True
        ps.code = codes.generate(db, "finished")
        db.commit()
        return RedirectResponse(f"/warehouse?view=finished&focus={ps.code}", status_code=303)
    return RedirectResponse("/warehouse?view=wip", status_code=303)


@router.post("/set/{set_id}/add-part")
async def set_add_part(
    set_id: int,
    part_id: int = Form(...),
    qty: str = Form("1"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Book a loose warehouse part (or `qty` units of it) onto this build. If
    fewer than the stock quantity are booked, the part is split: the remainder
    stays in loose stock and a new part with the booked quantity joins the set."""
    ps = db.get(PartSet, set_id)
    if not ps:
        return JSONResponse({"error": "not found"}, status_code=404)
    part = db.get(Part, part_id)
    if part and part.project_id is None and part.device_id is None and part.set_id is None:
        stock = part.quantity or 1
        n = max(1, min(_parse_int(qty) or 1, stock))
        if n >= stock:
            part.set_id = ps.id
        else:
            part.quantity = stock - n
            db.add(Part(
                name=part.name, set_id=ps.id, project_id=None, device_id=None,
                origin=part.origin, purchase_price=part.purchase_price,
                sale_price=part.sale_price, quantity=n,
                category_id=part.category_id, supplier_id=part.supplier_id,
                location_id=part.location_id, attributes=part.attributes,
                extra=part.extra, condition=part.condition, image_path=part.image_path,
                code=codes.generate(db, codes.part_prefix(part.category)),
            ))
        db.commit()
        _recompute_assembly_cost(db, ps)
        db.commit()
    return JSONResponse(_set_payload(db, ps))


@router.post("/set/{set_id}/remove-part")
async def set_remove_part(
    set_id: int,
    part_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Un-book a component part — it returns to loose warehouse stock."""
    ps = db.get(PartSet, set_id)
    if not ps:
        return JSONResponse({"error": "not found"}, status_code=404)
    part = db.get(Part, part_id)
    if part and part.set_id == ps.id:
        part.set_id = None
        db.commit()
        _recompute_assembly_cost(db, ps)
        db.commit()
    return JSONResponse(_set_payload(db, ps))


@router.post("/stock-from-project/{project_id}")
async def stock_from_project(
    project_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Register a finished project build as a sellable finished good on the
    shelf: an assembly (kind=assembly, sellable). Its component parts are booked
    onto it afterwards in the finished-good editor; cost then follows the parts."""
    from ..services import finance

    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/warehouse", status_code=303)
    f = finance.compute_project(db, project)
    title = project.title or project.name or "Finished good"
    ps = PartSet(
        name=title,
        kind=SetKind.assembly.value,
        sellable=True,
        purchase_price=round(f.material_cost, 2),  # estimate until parts booked
        sale_price=round(f.sale_price, 2),
        source_project_id=project.id,
        image_path=project.image_path,
        code=codes.generate(db, "finished"),
    )
    db.add(ps)
    db.commit()
    return RedirectResponse(f"/warehouse?focus={ps.code}", status_code=303)


@router.post("/set")
async def create_set(
    name: str = Form(...),
    purchase_price: str = Form(""),
    location_id: str = Form(""),
    free: str = Form(""),
    receipt: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create a purchase-lot set directly (bought together, one invoice). The
    set carries the total EK; its member parts are auto-created with their own
    VK but NO individual EK. Member parts stay available in Single parts."""
    is_free = free.strip().lower() in ("1", "on", "true", "yes")
    total = _parse_float(purchase_price) or 0.0
    rpath = None if is_free else save_receipt(receipt, "receipt")
    if not is_free and not rpath:
        return RedirectResponse(
            "/warehouse?view=sets&msg=Beleg erforderlich (oder als 'gratis' markieren)",
            status_code=303,
        )
    img_url, _ = save_image_or_error(image, "set")
    loc = _fk(location_id)
    exp_id = None
    if rpath:
        exp = exp_service.create(
            db, amount=total, expense_date=date.today(), vendor="",
            description=f"Set: {name.strip()}", category="Parts",
            project_id=None, receipt_path=rpath, bucket="warehouse",
        )
        exp_id = exp.id
    ps = PartSet(
        name=name.strip() or "Set",
        kind=SetKind.purchase_lot.value,
        purchase_price=total,
        expense_id=exp_id,
        location_id=loc,
        image_path=img_url,
        code=codes.generate(db, "set"),
    )
    db.add(ps)
    db.flush()
    for i, nm in enumerate(part_name):
        if not nm.strip():
            continue
        sale = _parse_float(part_sale[i]) if i < len(part_sale) else None
        db.add(Part(
            name=nm.strip(), set_id=ps.id, project_id=None, device_id=None,
            origin=PartOrigin.purchased, purchase_price=None,
            sale_price=sale or 0.0, quantity=1, location_id=loc,
            code=codes.generate(db, "part"),
        ))
    db.commit()
    return RedirectResponse(f"/warehouse?view=sets&focus={ps.code}", status_code=303)


@router.post("/set/{set_id}/update-lot")
async def update_lot(
    set_id: int,
    name: str = Form(...),
    purchase_price: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Update a purchase-lot set (total EK is set-level; parts have no own EK)."""
    ps = db.get(PartSet, set_id)
    if ps:
        new_image, _ = save_image_or_error(image, "set")
        if new_image:
            delete_image(ps.image_path)
            ps.image_path = new_image
        ps.name = name.strip()
        ps.purchase_price = _parse_float(purchase_price) or 0.0
        ps.location_id = _fk(location_id)
        ps.notes = notes.strip() or None
        # keep member locations in sync with the set
        for p in ps.parts:
            p.location_id = ps.location_id
        db.commit()
    return RedirectResponse("/warehouse?view=sets&msg=Saved", status_code=303)


@router.post("/set/{set_id}/member/add")
async def set_member_add(
    set_id: int,
    name: str = Form(""),
    sale: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    ps = db.get(PartSet, set_id)
    if ps:
        db.add(Part(
            name=(name.strip() or "Teil"), set_id=ps.id, project_id=None,
            device_id=None, origin=PartOrigin.purchased, purchase_price=None,
            sale_price=_parse_float(sale) or 0.0, quantity=1,
            location_id=ps.location_id, code=codes.generate(db, "part"),
        ))
        db.commit()
    return JSONResponse(_set_payload(db, ps))


@router.post("/set/{set_id}/member/{part_id}/save")
async def set_member_save(
    set_id: int,
    part_id: int,
    name: str = Form(...),
    sale: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    if part and part.set_id == set_id:
        if name.strip():
            part.name = name.strip()
        part.sale_price = _parse_float(sale) or 0.0
        db.commit()
    return JSONResponse(_set_payload(db, db.get(PartSet, set_id)))


@router.post("/set/{set_id}/member/{part_id}/remove")
async def set_member_remove(
    set_id: int,
    part_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    if part and part.set_id == set_id:
        db.delete(part)
        db.commit()
    return JSONResponse(_set_payload(db, db.get(PartSet, set_id)))


@router.post("/set/{set_id}/delete")
async def delete_set(
    set_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Delete a set/finished good. Its parts stay in the warehouse, unlinked."""
    ps = db.get(PartSet, set_id)
    if ps:
        for p in ps.parts:
            p.set_id = None
        db.delete(ps)
        db.commit()
    return RedirectResponse("/warehouse?msg=Deleted", status_code=303)
