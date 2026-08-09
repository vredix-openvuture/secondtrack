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


# 2 x 1 inch at 203 dpi — the native resolution of the common thermal label
# printers, so the image maps to printer dots one to one and needs no rescaling.
LABEL_DPI = 203
LABEL_W, LABEL_H = 2 * LABEL_DPI, 1 * LABEL_DPI
_FONT = "static/fonts/fredoka.ttf"


def label_png(payload: str, code: str, name: str, subtitle: str = "",
              fmt: str = "qr") -> bytes:
    """The finished 2x1in label as a bitmap.

    A browser print goes through the OS driver, which is where these Bluetooth
    thermal printers tend to lose the job and eject a blank label. A plain image
    at the printer's own resolution sidesteps that: it prints from the vendor
    app, from an image viewer, or straight through CUPS.
    """
    from PIL import Image, ImageDraw, ImageFont

    pad = 10
    im = Image.new("1", (LABEL_W, LABEL_H), 1)   # 1-bit: exactly what it prints
    d = ImageDraw.Draw(im)

    box = LABEL_H - 2 * pad
    if fmt == "barcode":
        art = Image.open(io.BytesIO(_barcode_png(code))).convert("1")
        art = art.resize((int(box * 1.05), box), Image.NEAREST)
    else:
        art = Image.open(io.BytesIO(qr_png(payload, box_size=10, border=1))).convert("1")
        art = art.resize((box, box), Image.NEAREST)
    im.paste(art, (pad, pad))

    x = pad + art.width + 12
    avail = LABEL_W - x - pad
    f_code = ImageFont.truetype(_FONT, 30)
    f_name = ImageFont.truetype(_FONT, 21)
    f_path = ImageFont.truetype(_FONT, 17)

    def wrap(text, font, width, max_lines):
        words, lines, cur = text.split(), [], ""
        for w in words:
            probe = f"{cur} {w}".strip()
            if d.textlength(probe, font=font) <= width or not cur:
                cur = probe
            else:
                lines.append(cur)
                cur = w
                if len(lines) == max_lines:
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        # Anything that did not fit is signalled rather than silently dropped.
        if lines and len(" ".join(lines)) < len(text):
            lines[-1] = lines[-1][: max(0, len(lines[-1]) - 1)] + "…"
        return lines

    y = pad
    d.text((x, y), code, font=f_code, fill=0)
    y += 34
    for line in wrap(name or "", f_name, avail, 2):
        d.text((x, y), line, font=f_name, fill=0)
        y += 24
    if subtitle:
        d.text((x, LABEL_H - pad - 20), wrap(subtitle, f_path, avail, 1)[0],
               font=f_path, fill=0)

    buf = io.BytesIO()
    im.save(buf, format="PNG", dpi=(LABEL_DPI, LABEL_DPI))
    return buf.getvalue()


def _barcode_png(payload: str) -> bytes:
    import barcode
    from barcode.writer import ImageWriter

    buf = io.BytesIO()
    barcode.get("code128", payload, writer=ImageWriter()).write(buf, options={
        "module_height": 12.0, "quiet_zone": 1.0, "write_text": False,
    })
    return buf.getvalue()


def label_pdf(payload: str, code: str, name: str, subtitle: str = "",
              fmt: str = "qr") -> bytes:
    """The same label as a PDF whose page is exactly 2x1 inch.

    A PNG carries its physical size only as metadata, which most image viewers
    ignore — they assume 96 dpi and the label comes out at twice the size or
    cropped. A PDF page size is binding, so the printer gets 2x1in either way.
    """
    from PIL import Image

    im = Image.open(io.BytesIO(label_png(payload, code, name, subtitle, fmt)))
    buf = io.BytesIO()
    # Greyscale rather than 1-bit: some RIPs refuse a 1-bit image inside a PDF.
    im.convert("L").save(buf, format="PDF", resolution=LABEL_DPI)
    return buf.getvalue()


def label_svg(payload: str, code: str, name: str, subtitle: str = "",
              fmt: str = "qr") -> str:
    """Vector version, 2x1in, for opening in a drawing program and printing
    from there — the route that is already known to work on this printer."""
    import html as _html

    w, h, pad = LABEL_W, LABEL_H, 10
    box = h - 2 * pad
    if fmt == "barcode":
        art, art_w = _barcode_svg_inner(code, box), int(box * 1.05)
    else:
        art, art_w = _qr_svg_inner(payload, box), box
    x = pad + art_w + 12

    def esc(t):
        return _html.escape(t or "")

    lines = [
        f'<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in" '
        f'viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="#fff"/>',
        f'<g transform="translate({pad},{pad})">{art}</g>',
        f'<text x="{x}" y="{pad + 26}" font-family="sans-serif" font-size="30" '
        f'font-weight="700" fill="#000">{esc(code)}</text>',
    ]
    y = pad + 52
    for chunk in _wrap_svg(name, 18, 2):
        lines.append(
            f'<text x="{x}" y="{y}" font-family="sans-serif" font-size="21" '
            f'fill="#000">{esc(chunk)}</text>'
        )
        y += 24
    if subtitle:
        lines.append(
            f'<text x="{x}" y="{h - pad - 4}" font-family="sans-serif" '
            f'font-size="17" fill="#333">{esc(subtitle[:34])}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines)


def _wrap_svg(text: str, per_line: int, max_lines: int) -> list[str]:
    words, out, cur = (text or "").split(), [], ""
    for word in words:
        probe = f"{cur} {word}".strip()
        if len(probe) <= per_line or not cur:
            cur = probe
        else:
            out.append(cur)
            cur = word
            if len(out) == max_lines:
                return out
    if cur and len(out) < max_lines:
        out.append(cur)
    return out


def _qr_svg_inner(payload: str, size: int) -> str:
    """QR as scaled rects — vector, so it stays crisp at any printer density."""
    import qrcode

    matrix = qrcode.QRCode(border=1)
    matrix.add_data(payload)
    matrix.make(fit=True)
    grid = matrix.get_matrix()
    n = len(grid)
    unit = size / n
    rects = "".join(
        f'<rect x="{c * unit:.2f}" y="{r * unit:.2f}" '
        f'width="{unit:.2f}" height="{unit:.2f}"/>'
        for r, row in enumerate(grid) for c, on in enumerate(row) if on
    )
    return f'<g fill="#000">{rects}</g>'


def _barcode_svg_inner(payload: str, height: int) -> str:
    """Code128 bars as rects, sized to the label rather than to millimetres."""
    import barcode

    bars = barcode.get("code128", payload).build()[0]
    unit = (height * 1.05) / len(bars)
    x, rects = 0.0, []
    for bit in bars:
        if bit == "1":
            rects.append(f'<rect x="{x:.2f}" y="0" width="{unit:.2f}" height="{height}"/>')
        x += unit
    return f'<g fill="#000">{"".join(rects)}</g>'
