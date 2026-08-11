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
from ..services import printing
from ..templating import ctx, templates

router = APIRouter()


def _base_url(request: Request, db: Session) -> str:
    """Absolute base URL for QR payloads: the `public_base_url` setting if set
    (needed behind a reverse proxy), else derived from the request."""
    configured = (get_setting(db, "public_base_url", "") or "").strip()
    return (configured or str(request.base_url)).rstrip("/")


@router.get("/scan")
async def scan_page(
    request: Request,
    code: str = "",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """In-app scanning: camera (BarcodeDetector, needs HTTPS) plus a manual
    field that doubles as the input for handheld USB/Bluetooth scanners."""
    code = code.strip()
    if code:
        kind, obj = codes.resolve(db, code)
        if obj is not None:
            return RedirectResponse(f"/s/{obj.code}", status_code=303)
        return templates.TemplateResponse(
            "scan.html", ctx(request, db, active="scan", not_found=code)
        )
    return templates.TemplateResponse(
        "scan.html", ctx(request, db, active="scan", not_found="")
    )


@router.get("/s/{code}")
async def scan_resolve(
    code: str,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    kind, obj = codes.resolve(db, code)
    if kind == "location":
        return RedirectResponse(f"/warehouse/locations?focus={obj.code}", status_code=303)
    # Assigned to a project means off the shelf: the warehouse list no longer
    # holds it, so send the scan where the object actually is.
    if obj is not None and getattr(obj, "project_id", None):
        return RedirectResponse(f"/projects/{obj.project_id}", status_code=303)
    if kind == "part" and obj is not None:
        # Merch is listed in its own department, so that is where a scan lands.
        view = "merch" if obj.is_merch else "parts"
        return RedirectResponse(f"/warehouse?view={view}&focus={obj.code}", status_code=303)
    if kind == "set" and obj is not None:
        view = "finished" if (obj.is_assembly or obj.sellable) else "sets"
        return RedirectResponse(f"/warehouse?view={view}&focus={obj.code}", status_code=303)
    return RedirectResponse("/warehouse?msg=Code not found", status_code=303)


def _label_parts(db, code: str, request):
    """(object, payload URL, subtitle) for any label format, or None."""
    kind, obj = codes.resolve(db, code)
    if obj is None:
        return None
    subtitle = obj.path if kind == "location" else (
        obj.location.path if getattr(obj, "location", None) else ""
    )
    return obj, f"{_base_url(request, db)}/s/{obj.code}", subtitle


@router.post("/label/{code}/print")
async def label_print(
    code: str,
    request: Request,
    fmt: str = "qr",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Print the label server-side via CUPS. The browser never renders anything
    here — its print pipeline is exactly what blanked labels on the D520 — so
    this works the same from a tablet as from a desktop."""
    from ..services import printing

    parts = _label_parts(db, code, request)
    if parts is None:
        return RedirectResponse("/warehouse?msg=Code not found", status_code=303)
    obj, url, subtitle = parts
    fmt = "barcode" if fmt == "barcode" else "qr"
    pdf = codes.label_pdf(url, obj.code, obj.name, subtitle, fmt)
    ok, msg = printing.print_pdf(db, pdf, obj.code)
    return RedirectResponse(
        f"/label/{obj.code}?fmt={fmt}&msg={msg}", status_code=303
    )


@router.get("/label/{code}.svg")
async def label_svg(
    code: str,
    request: Request,
    fmt: str = "qr",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Vector label, exactly 2x1in. Opens in a drawing program and prints from
    there — the route already proven to work on this printer."""
    from fastapi.responses import Response

    parts = _label_parts(db, code, request)
    if parts is None:
        return RedirectResponse("/warehouse?msg=Code not found", status_code=303)
    obj, url, subtitle = parts
    svg = codes.label_svg(url, obj.code, obj.name, subtitle,
                          "barcode" if fmt == "barcode" else "qr")
    return Response(svg, media_type="image/svg+xml", headers={
        "Content-Disposition": f'attachment; filename="{obj.code}.svg"',
    })


@router.get("/label/{code}.pdf")
async def label_pdf(
    code: str,
    request: Request,
    fmt: str = "qr",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """A PDF page is bindingly 2x1in, unlike a PNG whose dpi most viewers drop."""
    from fastapi.responses import Response

    parts = _label_parts(db, code, request)
    if parts is None:
        return RedirectResponse("/warehouse?msg=Code not found", status_code=303)
    obj, url, subtitle = parts
    pdf = codes.label_pdf(url, obj.code, obj.name, subtitle,
                          "barcode" if fmt == "barcode" else "qr")
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{obj.code}.pdf"',
    })


@router.get("/label/{code}.png")
async def label_png(
    code: str,
    request: Request,
    fmt: str = "qr",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """The label as a ready-to-print bitmap at the printer's own resolution.

    Browser printing hands the job to the OS driver, which is where Bluetooth
    thermal printers tend to eject a blank label. An image prints from the
    vendor app or an image viewer without that pipeline in between."""
    from fastapi.responses import Response

    kind, obj = codes.resolve(db, code)
    if obj is None:
        return RedirectResponse("/warehouse?msg=Code not found", status_code=303)
    subtitle = obj.path if kind == "location" else (
        obj.location.path if getattr(obj, "location", None) else ""
    )
    png = codes.label_png(
        f"{_base_url(request, db)}/s/{obj.code}", obj.code, obj.name, subtitle,
        "barcode" if fmt == "barcode" else "qr",
    )
    return Response(png, media_type="image/png", headers={
        "Content-Disposition": f'attachment; filename="{obj.code}.png"',
    })


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
            scan_url=url, print_ready=bool(printing.queue(db)),
        ),
    )
