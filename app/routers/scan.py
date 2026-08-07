"""Scan resolve + printable QR labels.

`/s/<code>` resolves a scan code to its object and redirects to it (so a phone
camera opens the right page). `/label/<code>` renders a printable label whose QR
encodes the absolute `/s/<code>` URL.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db, get_setting
from ..services import codes
from ..templating import ctx, templates

router = APIRouter()


def _base_url(request: Request, db: Session) -> str:
    """Absolute base URL for QR payloads: the `public_base_url` setting if set
    (needed behind a reverse proxy), else derived from the request."""
    configured = (get_setting(db, "public_base_url", "") or "").strip()
    return (configured or str(request.base_url)).rstrip("/")


@router.get("/s/{code}")
async def scan_resolve(
    code: str,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    kind, obj = codes.resolve(db, code)
    if kind == "location":
        return RedirectResponse(f"/warehouse/locations?focus={obj.code}", status_code=303)
    if kind == "part" and obj is not None:
        return RedirectResponse(f"/warehouse?view=parts&focus={obj.code}", status_code=303)
    if kind == "set" and obj is not None:
        view = "finished" if (obj.is_assembly or obj.sellable) else "sets"
        return RedirectResponse(f"/warehouse?view={view}&focus={obj.code}", status_code=303)
    return RedirectResponse("/warehouse?msg=Code not found", status_code=303)


@router.get("/label/{code}")
async def label(
    code: str,
    request: Request,
    fmt: str = "qr",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    kind, obj = codes.resolve(db, code)
    if obj is None:
        return RedirectResponse("/warehouse?msg=Code not found", status_code=303)

    name = obj.name
    if kind == "location":
        subtitle = obj.path
    else:  # part / set / finished good
        subtitle = obj.location.path if getattr(obj, "location", None) else ""

    fmt = "barcode" if fmt == "barcode" else "qr"
    url = f"{_base_url(request, db)}/s/{obj.code}"
    return templates.TemplateResponse(
        "warehouse/label.html",
        ctx(
            request, db, active="warehouse",
            code=obj.code, name=name, subtitle=subtitle, kind=kind, fmt=fmt,
            qr=codes.qr_data_uri(url), barcode=codes.barcode_svg(obj.code),
            scan_url=url,
        ),
    )
