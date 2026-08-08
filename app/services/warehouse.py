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
