from __future__ import annotations

import io
import os
import secrets

from fastapi import UploadFile

from ..config import get_settings

settings = get_settings()

# content-type -> extension, plus extension fallback (some browsers send
# application/octet-stream or empty content types).
_CT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MAX_BYTES = 25 * 1024 * 1024  # 25 MB accepted from the browser
MAX_EDGE = 1600               # longest side kept after compression
WEBP_QUALITY = 82
# A receipt is a document you have to read, so it keeps more detail.
RECEIPT_EDGE = 2400


def attempted(file: UploadFile | None) -> bool:
    """True if the user actually picked a file (vs. an empty form field)."""
    return bool(file is not None and file.filename)


def _ext_for(file: UploadFile) -> str | None:
    ct = (file.content_type or "").lower()
    if ct in _CT:
        return _CT[ct]
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext in _EXT:
        return ".jpg" if ext == ".jpeg" else ext
    return None


def compress(data: bytes, ext: str, max_edge: int = MAX_EDGE) -> tuple[bytes, str]:
    """Downscale to `max_edge` and re-encode as WebP.

    A phone photo is 3-5 MB and gets shown at a few hundred pixels; storing the
    original fills the data volume and every backup with nothing anyone sees.
    Animated GIFs are left alone (a still frame would lose the animation), and
    anything Pillow cannot read is passed through unchanged rather than lost.
    Returns (bytes, extension).
    """
    if ext == ".gif":
        return data, ext
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if getattr(im, "n_frames", 1) > 1:
                return data, ext
            # WebP has no CMYK/palette mode; RGB(A) covers both with alpha kept.
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
        encoded = out.getvalue()
    except Exception:  # noqa: BLE001 - an unreadable image is still the user's file
        return data, ext
    # A tiny PNG icon can come out larger as WebP; keep whichever is smaller.
    return (encoded, ".webp") if len(encoded) < len(data) else (data, ext)


def save_image(file: UploadFile | None, prefix: str) -> str | None:
    """Save an uploaded image and return its public URL path (/uploads/...).

    Returns None if no file was picked OR the file was rejected (wrong type /
    too large). Use attempted() to tell those apart for error messages."""
    if not attempted(file):
        return None
    ext = _ext_for(file)
    if not ext:
        return None

    data = file.file.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        return None
    data, ext = compress(data, ext)

    os.makedirs(settings.upload_dir, exist_ok=True)
    name = f"{prefix}-{secrets.token_hex(8)}{ext}"
    path = os.path.join(settings.upload_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return f"/uploads/{name}"


def save_image_or_error(file: UploadFile | None, prefix: str) -> tuple[str | None, str | None]:
    """Convenience wrapper: returns (url, error_message)."""
    url = save_image(file, prefix)
    if url is None and attempted(file):
        return None, "Bild abgelehnt – erlaubt: JPG/PNG/WebP/GIF/AVIF, max. 25 MB."
    return url, None


_RECEIPT_CT = dict(_CT)
_RECEIPT_CT["application/pdf"] = ".pdf"
_RECEIPT_EXT = set(_EXT) | {".pdf"}


def _receipt_ext(file: UploadFile) -> str | None:
    ct = (file.content_type or "").lower()
    if ct in _RECEIPT_CT:
        return _RECEIPT_CT[ct]
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext in _RECEIPT_EXT:
        return ".jpg" if ext == ".jpeg" else ext
    return None


def save_receipt(file: UploadFile | None, prefix: str = "receipt") -> str | None:
    """Save a receipt (PDF or image). Returns /uploads/... or None."""
    if not attempted(file):
        return None
    ext = _receipt_ext(file)
    if not ext:
        return None
    data = file.file.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        return None
    if ext != ".pdf":  # a photographed receipt is a phone photo like any other
        data, ext = compress(data, ext, RECEIPT_EDGE)
    os.makedirs(settings.upload_dir, exist_ok=True)
    name = f"{prefix}-{secrets.token_hex(8)}{ext}"
    with open(os.path.join(settings.upload_dir, name), "wb") as fh:
        fh.write(data)
    return f"/uploads/{name}"


def save_receipt_or_error(file: UploadFile | None, prefix: str = "receipt") -> tuple[str | None, str | None]:
    url = save_receipt(file, prefix)
    if url is None and attempted(file):
        return None, "Receipt rejected – allowed: PDF/JPG/PNG/WebP, max. 25 MB."
    return url, None


def read_upload(url_path: str | None) -> tuple[str, bytes] | None:
    """Read a stored upload back from disk as (filename, bytes)."""
    if not url_path or not url_path.startswith("/uploads/"):
        return None
    fname = os.path.basename(url_path.removeprefix("/uploads/"))
    fpath = os.path.join(settings.upload_dir, fname)
    try:
        with open(fpath, "rb") as fh:
            return fname, fh.read()
    except OSError:
        return None


def delete_image(url_path: str | None) -> None:
    if not url_path or not url_path.startswith("/uploads/"):
        return
    fname = os.path.basename(url_path.removeprefix("/uploads/"))
    try:
        os.remove(os.path.join(settings.upload_dir, fname))
    except OSError:
        pass
