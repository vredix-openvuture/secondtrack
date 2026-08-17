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
    """A money value from a form field, rounded to the cent.

    Everything the warehouse stores is money or a count, and money that is finer
    than a cent cannot be printed: an invoice line shows a two-decimal unit
    price, so a stored 13.3333 would leave the warehouse, the project and the
    document each with a different total. The form has always displayed the
    per-unit price to two decimals, so this stores what the user was shown.
    """
    if value is None or value.strip() == "":
        return None
    try:
        return round(float(value.replace(",", ".").strip()), 2)
    except ValueError:
        return None


def _per_unit(total: float | None, qty: int) -> float | None:
    """Split an entered total across `qty` units, at the cent.

    The remainder is dropped rather than hidden in the fractions: three units
    entered as 40.00 are stored at 13.33 and are worth 39.99 from then on,
    everywhere and consistently, instead of 40.00 here and 39.99 on the invoice.
    """
    if not total:
        return None
    return round(total / max(1, qty), 2)


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


class _RowForm:
    """Presents one set-member row's fields under the plain names the attribute
    extractor expects: it asks for `attr_<key>`, the row submitted them as
    `part_attr_<row>_<key>` so that N rows with N categories stay apart."""

    def __init__(self, form, prefix: str):
        self._form, self._prefix = form, prefix

    def get(self, name, default=None):
        return self._form.get(self._prefix + name.removeprefix("attr_"), default)


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



def _members_from_form(
    db, ps, form, *, names, sales, qtys, notes, categories, conditions,
    suppliers, locations, images, exp_id, costs=None, loc_default=None,
):
    """Create full-product member parts on `ps` from the set-member rows.

    Used by create (with a cost allocation) and by edit (costs=None: parts
    added later carry no own EK — the lot total stays at set level). Field
    names are row-scoped (part_attr_<row>_<key>), see _RowForm.
    """
    def pick(seq, i):
        return seq[i] if i < len(seq) else ""

    rows = [(i, nm) for i, nm in enumerate(names) if nm.strip()]
    for pos, (i, nm) in enumerate(rows):
        cat_id = _fk(pick(categories, i))
        category = db.get(Category, cat_id) if cat_id else None
        img = None
        if i < len(images) and images[i] is not None and images[i].filename:
            img, _ = save_image_or_error(images[i], "part")
        qty = max(1, int(_parse_float(pick(qtys, i)) or 1))
        sale = _parse_float(pick(sales, i)) or 0.0
        cost = costs[pos] if costs and pos < len(costs) else 0.0
        db.add(Part(
            name=nm.strip(),
            notes=(pick(notes, i) or "").strip() or None,
            set_id=ps.id, project_id=None, device_id=None,
            origin=PartOrigin.purchased,
            purchase_price=_per_unit(cost, qty),
            sale_price=_per_unit(sale, qty) or 0.0,
            quantity=qty,
            image_path=img,
            category_id=cat_id,
            condition=(pick(conditions, i) or "").strip() or None,
            supplier_id=_fk(pick(suppliers, i)),
            location_id=_fk(pick(locations, i)) or loc_default,
            attributes=wh.extract_attributes(category, _RowForm(form, f"part_attr_{pos}_")),
            source_expense_id=exp_id,
            code=codes.generate(db, codes.part_prefix(category)),
        ))


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
def warehouse_list(
    request: Request,
    cat: str = "",
    low: str = "",
    loc: str = "",
    sup: str = "",
    group: str = "",
    view: str = "parts",
    for_project: str = "",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    view = view if view in ("parts", "merch", "sets", "wip", "finished") else "parts"
    # Arrived from a project's "+ New": open the create dialog and hand the
    # finished item straight back to that project.
    for_pid = int(for_project) if for_project.strip().isdigit() else None
    # All warehouse parts (project & device NULL) — the basis for accounting and
    # set membership; the display list `parts` is this, optionally filtered.
    all_wh_parts = (
        db.query(Part)
        .filter(Part.project_id.is_(None), Part.device_id.is_(None))
        .order_by(Part.created_at.desc())
        .all()
    )
    active_cat = _fk(cat)
    active_sup = _fk(sup)
    active_loc = _fk(loc)
    group = group if group in ("location", "category") else ""
    only_low = low.strip() in ("1", "true", "yes")
    parts = all_wh_parts
    if active_cat:
        parts = [p for p in parts if p.category_id == active_cat]
    if active_sup:
        parts = [p for p in parts if p.supplier_id == active_sup]
    if active_loc:
        # Locations are a tree: picking the rack must include its shelves.
        loc_ids = {active_loc}
        all_locs = db.query(StorageLocation).all()
        grew = True
        while grew:
            grew = False
            for l in all_locs:
                if l.parent_id in loc_ids and l.id not in loc_ids:
                    loc_ids.add(l.id)
                    grew = True
        parts = [p for p in parts if p.location_id in loc_ids]
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
    #   merch        — stickers, shirts, cases: stock you hand out or sell, not
    #                  something you build with, so it has its own department
    #   lots         — purchase-lot sets (one invoice/EK; parts keep own VK)
    #   finished     — assemblies / finished goods (built from parts, consumed)
    # Sets assigned to a project have left the shelf, same as their parts.
    all_sets = (
        db.query(PartSet)
        .filter(PartSet.project_id.is_(None))
        .order_by(PartSet.created_at.desc())
        .all()
    )
    lots = [s for s in all_sets if s.kind == SetKind.purchase_lot.value]
    assemblies = [s for s in all_sets if s.kind == SetKind.assembly.value]
    wip = [s for s in assemblies if s.status == "wip"]
    finished = [s for s in assemblies if s.status != "wip"]
    lot_ids = {s.id for s in lots}
    assembly_ids = {s.id for s in assemblies}  # wip + finished members are consumed

    available = [p for p in parts if p.set_id is None or p.set_id in lot_ids]
    single_parts = [p for p in available if not p.is_merch]
    merch = [p for p in available if p.is_merch]

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

    # Merch accounting: what the merch on the shelf is worth, and what has
    # already been handed out for free — that money is advertising, and it is
    # the whole point of tracking merch separately.
    merch_cost = sum((p.purchase_price or 0.0) * (p.quantity or 1) for p in merch)
    merch_value = sum((p.sale_price or 0.0) * (p.quantity or 1) for p in merch)
    ad_cost = sum(
        (p.purchase_price or 0.0) * (p.quantity or 1)
        for p in db.query(Part).filter(Part.giveaway.is_(True)).all()
    )

    categories, suppliers, locations = _form_lists(db)
    low_stock = sum(1 for p in all_wh_parts if p.low_stock)
    counted = [p for p in all_wh_parts if p.set_id is None or p.set_id in lot_ids]
    single_count = sum(1 for p in counted if not p.is_merch)
    merch_count = sum(1 for p in counted if p.is_merch)
    # A part-specific filter hides the set-based departments.
    filtering = bool(active_cat or only_low or active_loc or active_sup)

    # Grouped rendering: one section per location (or category), each with its
    # own subtotal, so the shelf can be read shelf by shelf.
    def _grouped(items, mode):
        if mode == "location":
            key = lambda x: x.location.path if x.location else None  # noqa: E731
        elif mode == "category":
            key = lambda x: x.category.name if x.category else None  # noqa: E731
        else:
            return [{"label": None, "parts": items, "count": len(items), "cost": 0.0}]
        buckets: dict = {}
        for it in items:
            buckets.setdefault(key(it), []).append(it)
        labels = sorted((k for k in buckets if k is not None), key=str.lower)
        if None in buckets:
            labels.append(None)
        return [{
            "label": lb if lb is not None else "",
            "parts": buckets[lb],
            "count": len(buckets[lb]),
            "cost": sum((x.purchase_price or 0.0) * (x.quantity or 1) for x in buckets[lb]),
        } for lb in labels]

    part_groups = _grouped(single_parts, group)
    merch_groups = _grouped(merch, group)
    return templates.TemplateResponse(
        "warehouse/list.html",
        ctx(
            request, db, active="warehouse",
            view=view, single_count=single_count, merch_count=merch_count,
            single_parts=single_parts, finished=finished, lots=lots, wip=wip,
            merch=merch, merch_groups=merch_groups,
            merch_cost=merch_cost, merch_value=merch_value, ad_cost=ad_cost,
            projects=projects,
            stock_value=stock_value, stock_cost=stock_cost, low_stock=low_stock,
            categories=categories, suppliers=suppliers, locations=locations,
            for_project=for_pid,
            linkable_expenses=_linkable_expenses(db),
            optional_fields=wh.optional_fields(db),
            active_cat=active_cat, active_sup=active_sup, active_loc=active_loc,
            group=group, part_groups=part_groups,
            only_low=only_low, filtering=filtering, wh=wh,
            ebay_enabled=ebay.is_enabled(),
        ),
    )


@router.get("/price-suggest")
def price_suggest(
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
        "is_merch": bool(p.is_merch),
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
    is_merch: str = Form(""),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    part_purchase: list[str] = Form(default=[]),
    part_note: list[str] = Form(default=[]),
    part_image: list[UploadFile] = File(default=[]),
    part_receipt: list[UploadFile] = File(default=[]),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    expense_id: str = Form(""),
    for_project: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create one warehouse part — or, if set-parts are supplied, a set.

    With `for_project` the new item is assigned to that project right away and
    the user is sent back to it: a project never creates items itself, it sends
    you here and gets the result."""
    members = _set_members(
        part_name, part_sale, part_purchase, part_note, part_image, part_receipt
    )
    is_free = free.strip().lower() in ("1", "on", "true", "yes")
    merch = is_merch.strip().lower() in ("1", "on", "true", "yes")
    # Created from the merch department → land back in it.
    back = "/warehouse?view=merch" if merch else "/warehouse"
    sep = "&" if merch else "?"
    img_url, img_err = save_image_or_error(image, "part")
    sup_id, loc_id = _fk(supplier_id), _fk(location_id)
    # An existing receipt can stand in for an upload: link it instead of
    # creating a second expense for a purchase that is already booked.
    linked_exp = db.get(Expense, _fk(expense_id)) if _fk(expense_id) else None
    for_pid = _fk(for_project)
    if for_pid and not db.get(Project, for_pid):
        for_pid = None

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
        ps = _make_set(
            db, name=name.strip(), total=total,
            sale_price=_parse_float(sale_price), image_path=img_url,
            rpath=rpath, members=members, supplier_id=sup_id, location_id=loc_id,
            src_expense_id=linked_exp.id if linked_exp else None,
        )
        if for_pid:
            ps.project_id = for_pid
            for p in db.query(Part).filter(Part.set_id == ps.id).all():
                p.project_id = for_pid
            if ps.expense_id:
                exp = db.get(Expense, ps.expense_id)
                if exp and exp.project_id is None:
                    exp.project_id, exp.bucket = for_pid, "project"
            db.commit()
            return RedirectResponse(f"/projects/{for_pid}", status_code=303)
        return RedirectResponse(
            "/warehouse?msg=Set angelegt" + (f" — {img_err}" if img_err else ""),
            status_code=303,
        )

    rpath = None
    if not is_free and not linked_exp:
        rpath = save_receipt(receipt, "receipt")
        if not rpath:
            return RedirectResponse(
                back + sep + "msg=Beleg erforderlich (oder als 'gratis' markieren)",
                status_code=303,
            )

    qty = max(1, int(_parse_float(quantity) or 1))
    pp_total = None if is_free else _parse_float(purchase_price)
    sale_total = _parse_float(sale_price) or 0.0
    # Merch without a sale price exists to be handed out, so its purchase is an
    # advertising cost from the start — not stock waiting to be sold.
    promo = merch and not sale_total

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
        purchase_price=_per_unit(pp_total, qty),
        sale_price=_per_unit(sale_total, qty) or 0.0,
        image_path=img_url,
        quantity=qty,
        category_id=cat_id,
        supplier_id=sup_id,
        location_id=loc_id,
        attributes=attributes,
        extra=extra,
        condition=condition.strip() or None,
        is_merch=merch,
        code=codes.generate(
            db, codes.part_prefix(category) if category else ("MER" if merch else "part")
        ),
    )
    db.add(part)
    db.commit()
    if linked_exp:
        part.source_expense_id = linked_exp.id
        db.commit()
    elif rpath:
        exp = exp_service.create(
            db, amount=pp_total or 0.0, expense_date=date.today(), vendor="",
            description=("Merch: " if merch else "Part: ") + part.name,
            category="Advertising" if promo else ("Merch" if merch else "Parts"),
            project_id=None, receipt_path=rpath,
            bucket="advertisement" if promo else "warehouse",
        )
        part.source_expense_id = exp.id
        db.commit()
    if for_pid:
        part.project_id = for_pid
        wh.carry_expense_to_project(db, part, for_pid)
        db.commit()
        return RedirectResponse(f"/projects/{for_pid}", status_code=303)
    return RedirectResponse(
        back + (sep + f"msg={img_err}" if img_err else ""), status_code=303
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
        wh.carry_expense_to_project(db, part, project.id)  # and so does the cost
        db.commit()
        return RedirectResponse(f"/projects/{project.id}", status_code=303)
    return RedirectResponse("/warehouse", status_code=303)


@router.post("/{part_id}/book")
async def book_part(
    part_id: int,
    project_id: int = Form(...),
    qty: str = Form("1"),
    mode: str = Form("sold"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Book units of a stock item onto a project — the merch way: a quantity,
    and a choice between selling it with the build and handing it over for free.

    A free handout is not part of the build: it stays out of the project's
    material cost and sale value, and its purchase stays where it is, because
    the money spent on it is advertising (see finance.project_items).
    """
    part = db.get(Part, part_id)
    project = db.get(Project, project_id)
    view = "merch" if (part is not None and part.is_merch) else "parts"
    if not part or part.project_id is not None or part.device_id is not None or not project:
        return RedirectResponse(f"/warehouse?view={view}", status_code=303)
    free = mode.strip().lower() in ("free", "1", "on", "true", "yes")
    want = max(1, _parse_int(qty) or 1)
    booked, shared_receipt = wh.assign_units(
        db, part, want, project.id, carry_expense=not free
    )
    booked.giveaway = free
    db.commit()
    n = booked.quantity or 1
    if free:
        msg = f"{n}× {booked.name} gratis an {project.name} — als Werbekosten gebucht"
    elif shared_receipt:
        msg = f"{n}× {booked.name} an {project.name} — der Beleg deckt mehrere Objekte ab und bleibt im Lager"
    else:
        msg = f"{n}× {booked.name} an {project.name} gebucht"
    return RedirectResponse(f"/warehouse?view={view}&msg={msg}", status_code=303)


@router.post("/{part_id}/merch")
async def toggle_merch(
    part_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Move a stock item between the parts and the merch department."""
    part = db.get(Part, part_id)
    if not part or part.project_id is not None:
        return RedirectResponse("/warehouse", status_code=303)
    part.is_merch = not part.is_merch
    db.commit()
    return RedirectResponse(
        "/warehouse?view=" + ("merch" if part.is_merch else "parts"), status_code=303
    )


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
                is_merch=part.is_merch,
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
    onto it afterwards in the finished-good editor; cost then follows the parts.

    Only project types that declare shop_stock — customer work is built for one
    customer and invoiced, so it never becomes shop stock."""
    from ..services import finance

    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/warehouse", status_code=303)
    if not (project.type and project.type.shop_stock):
        return RedirectResponse(
            f"/projects/{project_id}?msg=Dieser Projekttyp erzeugt keine Shop-Ware — er wird abgerechnet",
            status_code=303,
        )
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
    request: Request,
    name: str = Form(...),
    purchase_price: str = Form(""),
    location_id: str = Form(""),
    free: str = Form(""),
    convert_part_id: str = Form(""),
    receipt: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    part_qty: list[str] = Form(default=[]),
    part_note: list[str] = Form(default=[]),
    part_category: list[str] = Form(default=[]),
    part_condition: list[str] = Form(default=[]),
    part_supplier: list[str] = Form(default=[]),
    part_location: list[str] = Form(default=[]),
    part_image: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Create a purchase-lot set directly (bought together, one invoice).

    A member of a lot is a full warehouse product, not just a name: it carries
    its own category (with that category's fields), condition, supplier,
    location, quantity, image and note, exactly like a part created on its own.
    The set holds the total EK — individual purchase prices would double-count
    the same invoice — and it is split across the members by sale value."""
    is_free = free.strip().lower() in ("1", "on", "true", "yes")
    total = _parse_float(purchase_price) or 0.0
    # Converting a part that should have been a set: its receipt, image and
    # location seed the set, and the part itself is absorbed at the end.
    conv = None
    conv_id = _fk(convert_part_id)
    if conv_id:
        cp = db.get(Part, conv_id)
        if cp is not None and cp.project_id is None and cp.set_id is None:
            conv = cp
    rpath = None if is_free else save_receipt(receipt, "receipt")
    if not is_free and not rpath and conv is None:
        return RedirectResponse(
            "/warehouse?view=sets&msg=Beleg erforderlich (oder als 'gratis' markieren)",
            status_code=303,
        )
    img_url, _ = save_image_or_error(image, "set")
    if not img_url and conv is not None:
        img_url = conv.image_path
    loc = _fk(location_id)
    if loc is None and conv is not None:
        loc = conv.location_id
    exp_id = None
    if rpath:
        exp = exp_service.create(
            db, amount=total, expense_date=date.today(), vendor="",
            description=f"Set: {name.strip()}", category="Parts",
            project_id=None, receipt_path=rpath, bucket="warehouse",
        )
        exp_id = exp.id
    elif conv is not None and conv.source_expense_id:
        exp_id = conv.source_expense_id
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

    form = await request.form()
    rows = [(i, nm) for i, nm in enumerate(part_name) if nm.strip()]
    # The lot total is split by sale value, so each member carries its share of
    # the one invoice instead of an invented price of its own.
    sales = [_parse_float(part_sale[i]) if i < len(part_sale) else 0.0 for i, _ in rows]
    costs = _allocate(total, [x or 0.0 for x in sales])
    _members_from_form(
        db, ps, form,
        names=part_name, sales=part_sale, qtys=part_qty, notes=part_note,
        categories=part_category, conditions=part_condition,
        suppliers=part_supplier, locations=part_location, images=part_image,
        exp_id=exp_id, costs=costs, loc_default=loc,
    )
    if conv is not None:
        # The set may have taken the part's image path — then the file lives on
        # under the set. A freshly uploaded one orphans the part's old file.
        if conv.image_path and img_url != conv.image_path:
            delete_image(conv.image_path)
        db.delete(conv)
    db.commit()
    return RedirectResponse(f"/warehouse?view=sets&focus={ps.code}", status_code=303)


@router.post("/set/{set_id}/update-lot")
async def update_lot(
    set_id: int,
    request: Request,
    name: str = Form(...),
    purchase_price: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    part_name: list[str] = Form(default=[]),
    part_sale: list[str] = Form(default=[]),
    part_qty: list[str] = Form(default=[]),
    part_note: list[str] = Form(default=[]),
    part_category: list[str] = Form(default=[]),
    part_condition: list[str] = Form(default=[]),
    part_supplier: list[str] = Form(default=[]),
    part_location: list[str] = Form(default=[]),
    part_image: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Update a purchase-lot set (total EK is set-level; parts have no own EK).

    Parts added here use the same full member rows as the create dialog —
    category with its fields, condition, supplier, location, quantity, image,
    note. They join the existing lot, so they carry no own EK."""
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
        db.flush()
        _members_from_form(
            db, ps, await request.form(),
            names=part_name, sales=part_sale, qtys=part_qty, notes=part_note,
            categories=part_category, conditions=part_condition,
            suppliers=part_supplier, locations=part_location, images=part_image,
            exp_id=ps.expense_id, costs=None, loc_default=ps.location_id,
        )
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
