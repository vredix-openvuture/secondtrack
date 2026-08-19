"""Warehouse domain helpers: category field schemas and part attributes.

A category owns an ordered list of field definitions (its schema). A part that
belongs to a category stores the concrete values in `Part.attributes` as a JSON
object keyed by field key. This module builds/validates schemas and extracts &
coerces attribute values from submitted forms.
"""
from __future__ import annotations

import json
import re

from ..models import Category

FIELD_TYPES = ("text", "number", "select", "bool", "date")

# Global optional fields (same for every product, editable in Settings). Values
# are stored in Part.extra. This is the default schema seeded on first use.
DEFAULT_OPTIONAL_FIELDS = [
    {"key": "serial_no", "label": "Serial no.", "type": "text", "options": [], "required": False, "unit": ""},
    {"key": "mpn", "label": "MPN", "type": "text", "options": [], "required": False, "unit": ""},
    {"key": "ean", "label": "EAN", "type": "text", "options": [], "required": False, "unit": ""},
    {"key": "unit", "label": "Unit (pcs, m…)", "type": "text", "options": [], "required": False, "unit": ""},
    {"key": "min_stock", "label": "Reorder level", "type": "number", "options": [], "required": False, "unit": ""},
    {"key": "purchase_date", "label": "Purchase date", "type": "date", "options": [], "required": False, "unit": ""},
    {"key": "warranty_until", "label": "Warranty until", "type": "date", "options": [], "required": False, "unit": ""},
]


def optional_fields(db) -> list[dict]:
    """The global optional-field schema (from Settings, else the default seed)."""
    from ..db import get_setting

    raw = get_setting(db, "optional_fields_json", None)
    if not raw:
        return DEFAULT_OPTIONAL_FIELDS
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else DEFAULT_OPTIONAL_FIELDS
    except (ValueError, TypeError):
        return DEFAULT_OPTIONAL_FIELDS


def _clean_key(source: str, used: set[str]) -> str:
    """Make a stable, unique snake_case key from a label or existing key."""
    base = re.sub(r"[^a-z0-9]+", "_", (source or "").strip().lower()).strip("_")
    base = base or "field"
    key, i = base, 2
    while key in used:
        key = f"{base}_{i}"
        i += 1
    return key


def sanitize_fields(raw: str) -> str:
    """Validate a client-supplied field-schema JSON and return canonical JSON.

    Keeps provided keys (cleaned + de-duplicated) so existing part attributes
    stay linked; drops empty/invalid rows. Never raises."""
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        data = []
    if not isinstance(data, list):
        data = []

    out: list[dict] = []
    used: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        if not label:
            continue
        ftype = item.get("type", "text")
        if ftype not in FIELD_TYPES:
            ftype = "text"
        key = _clean_key(str(item.get("key") or label), used)
        used.add(key)
        opts = item.get("options") or []
        if isinstance(opts, str):
            opts = re.split(r"[\n,]", opts)
        if not isinstance(opts, list):
            opts = []
        opts = [str(o).strip() for o in opts if str(o).strip()]
        out.append({
            "key": key,
            "label": label,
            "type": ftype,
            "options": opts if ftype == "select" else [],
            "required": bool(item.get("required")),
            "unit": str(item.get("unit", "")).strip(),
        })
    return json.dumps(out, ensure_ascii=False)


def _coerce(ftype: str, raw):
    if isinstance(raw, str):
        raw = raw.strip()
    if raw in (None, ""):
        return None
    if ftype == "number":
        try:
            n = float(str(raw).replace(",", "."))
            return int(n) if n == int(n) else n
        except ValueError:
            return None
    if ftype == "bool":
        return str(raw).lower() in ("1", "on", "true", "yes")
    return raw


def _is_checked(form, name) -> bool:
    raw = form.get(name)
    return raw not in (None, "", "0", "false", "off")


def extract_values(fields: list[dict], form, prefix: str) -> dict:
    """Read `<prefix><key>` values from a submitted form for the given field
    schema and return a {key: coerced_value} dict (empty values dropped)."""
    result: dict = {}
    for field in fields:
        key = field.get("key")
        if not key:
            continue
        if field.get("type") == "bool":
            val = _is_checked(form, f"{prefix}{key}")
        else:
            val = _coerce(field.get("type", "text"), form.get(f"{prefix}{key}"))
        if val not in (None, "", [], False):
            result[key] = val
    return result


def extract_attributes(category: Category | None, form) -> str | None:
    """Category-specific field values (`attr_<key>`) → JSON, or None if empty."""
    if category is None:
        return None
    result = extract_values(category.fields, form, "attr_")
    return json.dumps(result, ensure_ascii=False) if result else None


def extract_extra(fields: list[dict], form) -> str:
    """Global optional field values (`opt_<key>`) → JSON (always a string)."""
    result = extract_values(fields, form, "opt_")
    return json.dumps(result, ensure_ascii=False)


def display_attributes(part) -> list[tuple[str, str]]:
    """(label, value) pairs for a part's attributes, in category field order."""
    cat = part.category
    if cat is None:
        return []
    values = part.attrs
    out: list[tuple[str, str]] = []
    for field in cat.fields:
        key = field.get("key")
        if key not in values:
            continue
        val = values[key]
        if field.get("type") == "bool":
            val = "✓" if val else "—"
        unit = field.get("unit")
        text = f"{val} {unit}".strip() if unit else str(val)
        out.append((field.get("label", key), text))
    return out


def carry_expense_to_project(db, part, project_id: int) -> bool:
    """Move a part's purchase expense onto the project it was just installed in,
    so the cost is booked where the part is used instead of staying in the
    warehouse bucket.

    Skipped when the expense also covers other parts or a whole set: one receipt
    for a purchase lot must not land entirely on one project. Returns whether
    the expense moved; the caller commits.
    """
    from ..models import Expense, Part, PartSet

    if not part.source_expense_id:
        return False
    exp = db.get(Expense, part.source_expense_id)
    if exp is None or exp.project_id is not None:
        return False
    shared = db.query(Part).filter(
        Part.source_expense_id == exp.id, Part.id != part.id
    ).count()
    shared += db.query(PartSet).filter(PartSet.expense_id == exp.id).count()
    if shared:
        return False
    exp.project_id = project_id
    exp.bucket = "project"
    return True


def _shelf_sibling(db, part):
    """The warehouse row a project row was split off from: same product, same
    purchase, still on the shelf. Units move back into it instead of piling up
    duplicate rows for one product."""
    from ..models import Part

    q = db.query(Part).filter(
        Part.id != part.id,
        Part.project_id.is_(None),
        Part.set_id.is_(None),
        Part.name == part.name,
    )
    if part.source_expense_id:
        q = q.filter(Part.source_expense_id == part.source_expense_id)
    else:
        q = q.filter(Part.source_expense_id.is_(None))
    return q.first()


def _split_off(db, part, qty: int, project_id: int | None):
    """A copy of `part` holding `qty` units, on `project_id` (None = shelf).
    Prices are per unit, so they carry over untouched."""
    from ..models import Part

    from . import codes

    # `giveaway` is deliberately not copied: it says how *this* booking was
    # handed over, not what the product is, so a split always starts neutral.
    clone = Part(
        name=part.name, notes=part.notes, image_path=part.image_path,
        project_id=project_id, source_expense_id=part.source_expense_id,
        category_id=part.category_id, supplier_id=part.supplier_id,
        location_id=part.location_id, attributes=part.attributes,
        extra=part.extra, origin=part.origin, condition=part.condition,
        serial_no=part.serial_no, mpn=part.mpn, ean=part.ean,
        warranty_until=part.warranty_until, purchase_date=part.purchase_date,
        min_stock=part.min_stock, unit=part.unit, is_merch=part.is_merch,
        purchase_price=part.purchase_price, sale_price=part.sale_price,
        quantity=qty, code=codes.generate(db, "part"),
    )
    db.add(clone)
    return clone


def assign_units(
    db, part, qty: int, project_id: int, *, carry_expense: bool = True
) -> tuple[object, bool]:
    """Book `qty` units of a shelf part onto a project. Booking every unit moves
    the row and takes the purchase expense along. Booking fewer splits the row
    and leaves the expense on the shelf — one receipt covering ten units must
    not land whole on a project that took three.

    `carry_expense=False` for a free handout: the purchase stays where it is,
    because a gift is an advertising cost, not a cost of that build.

    Returns (project row, whether a receipt was left behind unlinked).
    """
    have = part.quantity or 1
    qty = max(1, min(int(qty), have))
    if qty >= have:
        part.project_id = project_id
        part.device_id = None
        moved = carry_expense and carry_expense_to_project(db, part, project_id)
        return part, carry_expense and bool(part.source_expense_id) and not moved
    booked = _split_off(db, part, qty, project_id)
    part.quantity = have - qty
    return booked, carry_expense and bool(part.source_expense_id)


def set_booked_units(db, part, qty: int) -> None:
    """Change how many units of a project item are booked. Units taken off go
    back to the shelf row they came from (recreated if it is gone); units added
    come from that row, capped by what is actually in stock. Zero releases the
    item entirely."""
    current = part.quantity or 1
    qty = max(0, int(qty))
    if qty == current:
        return
    sibling = _shelf_sibling(db, part)
    if qty < current:
        back = current - qty
        if sibling:
            sibling.quantity = (sibling.quantity or 0) + back
        else:
            _split_off(db, part, back, None)
        if qty == 0:
            db.delete(part)
        else:
            part.quantity = qty
        return
    available = (sibling.quantity or 0) if sibling else 0
    take = min(qty - current, available)
    if take <= 0:
        return
    sibling.quantity = available - take
    part.quantity = current + take
    if sibling.quantity <= 0:
        db.delete(sibling)


def stock_totals(db) -> dict:
    """What the shelf is worth, counted once and the same way everywhere.

    This lived twice: the warehouse page counted quantities, lot totals and
    assembly costs, while the statistics page summed each part's unit price and
    ignored both the quantity and the sets. Nine fans counted as one fan there,
    and a purchase lot counted as whatever its members happened to carry, so the
    two pages disagreed about the same shelf.

    Counted, over everything with no project on it:
      cost   loose bought parts x quantity, plus each lot's total, plus each
             assembly's cost. WIP ties up material as much as a finished good.
      value  sale price x quantity of every part not consumed into an assembly,
             plus each finished good's price. A WIP build has no sale value; it
             is not sellable yet.

    Set members are not added on top of their set: a lot's total is the invoice
    it came from, and an assembly's cost is recomputed from its members, so
    counting both would bill the same purchase twice.
    """
    from ..models import Part, PartOrigin, PartSet, SetKind

    parts = (
        db.query(Part)
        .filter(Part.project_id.is_(None), Part.device_id.is_(None))
        .all()
    )
    sets = db.query(PartSet).filter(PartSet.project_id.is_(None)).all()
    lots = [s for s in sets if s.kind == SetKind.purchase_lot.value]
    assemblies = [s for s in sets if s.kind == SetKind.assembly.value]
    finished = [s for s in assemblies if s.status != "wip"]
    assembly_ids = {s.id for s in assemblies}

    cost = sum(
        (p.purchase_price or 0.0) * (p.quantity or 1)
        for p in parts
        if p.set_id is None and p.origin == PartOrigin.purchased
    ) + sum((s.purchase_price or 0.0) for s in lots) \
      + sum((s.purchase_price or 0.0) for s in assemblies)

    value = sum(
        (p.sale_price or 0.0) * (p.quantity or 1)
        for p in parts
        if p.set_id not in assembly_ids
    ) + sum((s.sale_price or 0.0) for s in finished)

    return {
        "cost": round(cost, 2),
        "value": round(value, 2),
        "parts": sum(p.quantity or 1 for p in parts if p.set_id not in assembly_ids),
        "low_stock": sum(1 for p in parts if p.low_stock),
    }
