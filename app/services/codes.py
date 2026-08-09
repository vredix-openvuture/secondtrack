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
QUIET_MODULES = 10   # Code128 clear space either side, in module widths
LABEL_W, LABEL_H = 2 * LABEL_DPI, 1 * LABEL_DPI
_FONT = "static/fonts/fredoka.ttf"


def label_png(payload: str, code: str, name: str, subtitle: str = "",
              fmt: str = "qr") -> bytes:
    """The finished 2x1in label as a bitmap.

    The code is drawn module by module at a whole number of pixels. Rendering it
    as an image and resizing to fit lands on a fractional module width, and
    since a nearest-neighbour resize cannot split a pixel some modules come out
    a pixel wider than others — an irregular grid that a scanner rejects.
    """
    from PIL import Image, ImageDraw, ImageFont

    pad = 16          # inner margin, kept generous so nothing crowds the edge
    gap = 14          # between the code and the text column
    im = Image.new("L", (LABEL_W, LABEL_H), 255)   # greyscale: clean glyphs
    d = ImageDraw.Draw(im)

    box = LABEL_H - 2 * pad
    f_code = ImageFont.truetype(_FONT, 30)
    f_name = ImageFont.truetype(_FONT, 21)
    f_path = ImageFont.truetype(_FONT, 17)

    if fmt == "barcode":
        # Code128 needs 123 modules for an 8-character code. Beside the text
        # that is 0.18mm per module — below the standard minimum and finer than
        # a 203dpi dot, so nothing reads it. It gets the full width instead and
        # the text sits above.
        return _label_barcode(im, d, code, name, pad, f_code, f_name)

    art_w = _draw_qr(d, payload, pad, pad, box)
    x = pad + art_w + gap
    avail = LABEL_W - x - pad

    def wrap(text, font, width, max_lines):
        words, lines, cur = (text or "").split(), [], ""
        for w in words:
            probe = f"{cur} {w}".strip()
            if d.textlength(probe, font=font) <= width or not cur:
                cur = probe
            else:
                lines.append(cur)
                cur = w
                if len(lines) == max_lines:
                    return lines
        if cur and len(lines) < max_lines:
            lines.append(cur)
        return lines

    y = pad
    d.text((x, y), code, font=f_code, fill=0)
    y += 36
    d.line([(x, y), (LABEL_W - pad, y)], fill=0, width=2)
    y += 8
    for line in wrap(name, f_name, avail, 2):
        d.text((x, y), line, font=f_name, fill=0)
        y += 24
    if subtitle:
        line = wrap(subtitle, f_path, avail, 1)
        if line:
            d.text((x, LABEL_H - pad - 18), line[0], font=f_path, fill=0)

    # Threshold last: the glyphs are rasterised with antialiasing first, then
    # reduced in one step, which keeps them legible at this size.
    buf = io.BytesIO()
    im.point(lambda v: 0 if v < 160 else 255, mode="1").save(
        buf, format="PNG", dpi=(LABEL_DPI, LABEL_DPI)
    )
    return buf.getvalue()


def _label_barcode(im, d, code: str, name: str, pad: int, f_code, f_name) -> bytes:
    """Stacked layout: identifier and name on top, barcode across the width."""
    d.text((pad, pad - 2), code, font=f_code, fill=0)
    if name:
        cw = int(d.textlength(code, font=f_code))
        d.line([(pad + cw + 11, pad + 2), (pad + cw + 11, pad + 32)], fill=0, width=2)
        w = LABEL_W - 2 * pad - cw - 22
        text = name
        while text and d.textlength(text, font=f_name) > w:
            text = text[:-1]
        if text != name:
            text = text[:-1] + "…"
        d.text((pad + cw + 20, pad + 8), text, font=f_name, fill=0)
    top = pad + 38
    _draw_barcode(d, code, pad, top, LABEL_W - 2 * pad, LABEL_H - top - pad)
    buf = io.BytesIO()
    im.point(lambda v: 0 if v < 160 else 255, mode="1").save(
        buf, format="PNG", dpi=(LABEL_DPI, LABEL_DPI)
    )
    return buf.getvalue()


def _draw_qr(d, payload: str, x: int, y: int, box: int) -> int:
    """Draw the QR as whole-pixel modules; returns the width actually used."""
    import qrcode

    q = qrcode.QRCode(border=1)
    q.add_data(payload)
    q.make(fit=True)
    grid = q.get_matrix()
    n = len(grid)
    unit = max(1, box // n)          # whole pixels per module, never fractional
    size = unit * n
    off = (box - size) // 2          # centre the rounding slack
    for r, row in enumerate(grid):
        for c, on in enumerate(row):
            if on:
                px, py = x + off + c * unit, y + off + r * unit
                d.rectangle([px, py, px + unit - 1, py + unit - 1], fill=0)
    return box


def _draw_barcode(d, payload: str, x: int, y: int, width: int, height: int) -> int:
    """Code128 bars at whole-pixel width, same reasoning as the QR."""
    import barcode

    bars = barcode.get("code128", payload).build()[0]
    unit = max(1, width // (len(bars) + 2 * QUIET_MODULES))
    left = x + QUIET_MODULES * unit
    for i, bit in enumerate(bars):
        if bit == "1":
            px = left + i * unit
            d.rectangle([px, y, px + unit - 1, y + height - 1], fill=0)
    return unit * (len(bars) + 2 * QUIET_MODULES)


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

    # Same generous inner margin as the bitmap: the code was sitting too close
    # to the top edge, which reads as a printing error even when it is not.
    w, h, pad = LABEL_W, LABEL_H, 16
    box = h - 2 * pad

    def esc(t):
        return _html.escape(t or "")

    if fmt == "barcode":
        # Full width, text above — see _label_barcode for why.
        top = pad + 38
        return "\n".join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" width="2in" height="1in" '
            f'viewBox="0 0 {w} {h}">',
            f'<rect width="{w}" height="{h}" fill="#fff"/>',
            f'<text x="{pad}" y="{pad + 24}" font-family="sans-serif" '
            f'font-size="30" font-weight="700" fill="#000">{esc(code)}</text>',
            f'<line x1="{pad + 140}" y1="{pad + 2}" x2="{pad + 140}" y2="{pad + 30}" '
            f'stroke="#000" stroke-width="2"/>',
            f'<text x="{pad + 150}" y="{pad + 24}" font-family="sans-serif" '
            f'font-size="19" fill="#000">{esc((name or "")[:20])}</text>',
            f'<g transform="translate({pad},{top})">'
            f'{_barcode_svg_inner(code, h - top - pad, w - 2 * pad)}</g>',
            "</svg>",
        ])

    art, art_w = _qr_svg_inner(payload, box), box
    x = pad + art_w + 14

    lines = [
        f'<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in" '
        f'viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="#fff"/>',
        f'<g transform="translate({pad},{pad})">{art}</g>',
        f'<text x="{x}" y="{pad + 24}" font-family="sans-serif" font-size="30" '
        f'font-weight="700" fill="#000">{esc(code)}</text>',
        f'<line x1="{x}" y1="{pad + 33}" x2="{w - pad}" y2="{pad + 33}" '
        f'stroke="#000" stroke-width="2"/>',
    ]
    y = pad + 54
    for chunk in _wrap_svg(name, 17, 2):
        lines.append(
            f'<text x="{x}" y="{y}" font-family="sans-serif" font-size="21" '
            f'fill="#000">{esc(chunk)}</text>'
        )
        y += 24
    if subtitle:
        lines.append(
            f'<text x="{x}" y="{h - pad - 2}" font-family="sans-serif" '
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


def _barcode_svg_inner(payload: str, height: int, width: int) -> str:
    """The library's own barcode, embedded as an image.

    Drawing the bars by hand from build() reproduces that string faithfully and
    still will not decode — python-barcode's writer does more than paint the
    pattern. Rather than reverse-engineer it, the label embeds what the library
    renders, which is verified to scan. It is a bitmap inside the SVG, but a
    barcode nothing can read is worse than one that is not vector.
    """
    b64 = base64.b64encode(_barcode_png(payload)).decode()
    return (
        f'<image x="0" y="0" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" '
        f'xlink:href="data:image/png;base64,{b64}" '
        f'href="data:image/png;base64,{b64}"/>'
    )
