"""Scan codes + labels (QR / barcode) for warehouse objects.

Every scannable object (part, set/finished good, storage location) carries a
short, human-readable code such as ``CPU-3K7Q``: a 3-character descriptor (the
category for parts, or the object type) + ``-`` + a 4-character unique suffix
drawn from an unambiguous alphabet (no 0/O, 1/I/L). ``/s/<code>`` resolves a
code back to its object, so a phone camera scanning the QR opens the right page.
"""
from __future__ import annotations

import base64
import io
import re
import secrets

from sqlalchemy.orm import Session

from ..models import Part, PartSet, StorageLocation

# Crockford-ish alphabet without visually ambiguous characters.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
BODY_LEN = 4

# Default 3-letter descriptors per object type (used when no better one exists).
_KIND_PREFIX = {
    "part": "PRT",
    "set": "SET",
    "wip": "WIP",
    "finished": "PRD",
    "location": "LOC",
}


def _clean3(text: str | None, default: str = "GEN") -> str:
    """A 3-char uppercase descriptor from arbitrary text, else the default."""
    s = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    return s[:3] if len(s) >= 3 else default


def part_prefix(category) -> str:
    """Descriptor for a part: its category name (e.g. CPU), else PRT."""
    if category is not None and getattr(category, "name", None):
        return _clean3(category.name, "PRT")
    return "PRT"


def _exists(db: Session, code: str) -> bool:
    return bool(
        db.query(Part).filter(Part.code == code).first()
        or db.query(PartSet).filter(PartSet.code == code).first()
        or db.query(StorageLocation).filter(StorageLocation.code == code).first()
    )


def generate(db: Session, prefix: str) -> str:
    """A fresh, collision-checked code ``PREFIX-XXXX``. `prefix` may be an object
    kind ('part'/'set'/'finished'/'location') or a custom 3-char descriptor."""
    pfx = _clean3(_KIND_PREFIX.get(prefix, prefix), "GEN")
    for _ in range(60):
        body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LEN))
        code = f"{pfx}-{body}"
        if not _exists(db, code):
            return code
    return f"{pfx}-{secrets.token_hex(3).upper()}"  # astronomically unlikely


def ensure(db: Session, obj, kind: str) -> str:
    """Make sure `obj` has a code, assigning (and flushing) one if missing."""
    if not getattr(obj, "code", None):
        obj.code = generate(db, kind)
        db.flush()
    return obj.code


def resolve(db: Session, code: str):
    """Resolve a scan code to (kind, object) or (None, None)."""
    code = (code or "").strip().upper()
    if not code:
        return None, None
    part = db.query(Part).filter(Part.code == code).first()
    if part:
        return "part", part
    ps = db.query(PartSet).filter(PartSet.code == code).first()
    if ps:
        return "set", ps
    loc = db.query(StorageLocation).filter(StorageLocation.code == code).first()
    if loc:
        return "location", loc
    return None, None


def qr_png(payload: str, box_size: int = 8, border: int = 2) -> bytes:
    """Render `payload` (usually a URL) as a QR-code PNG."""
    import qrcode

    img = qrcode.make(payload, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_data_uri(payload: str, box_size: int = 8, border: int = 2) -> str:
    b64 = base64.b64encode(qr_png(payload, box_size, border)).decode()
    return f"data:image/png;base64,{b64}"


def barcode_svg(payload: str) -> str:
    """A thin Code128 1-D barcode (SVG) for narrow label rolls (~20 mm wide).
    Encodes the raw code so a handheld scanner reads it directly."""
    import barcode
    from barcode.writer import SVGWriter

    code = barcode.get("code128", payload, writer=SVGWriter())
    buf = io.BytesIO()
    code.write(buf, options={
        "module_width": 0.3,     # mm per narrow bar → keeps it slim
        "module_height": 15.0,   # bar height in mm
        "quiet_zone": 1.0,
        "write_text": False,     # the identifier is printed beside it
    })
    return buf.getvalue().decode("utf-8")
