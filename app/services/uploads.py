from __future__ import annotations

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
MAX_BYTES = 25 * 1024 * 1024  # 25 MB


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
