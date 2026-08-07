"""Storage-location management (Warehouse › Locations).

Locations are hierarchical (room → rack → shelf → bin) via `parent_id`. Every
location carries a scan code so a printable barcode/QR label can be generated.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Part, PartSet, StorageLocation
from ..services import codes
from ..templating import ctx, templates

router = APIRouter(prefix="/warehouse/locations")


def _fk(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _descendant_ids(loc: StorageLocation, seen: set[int] | None = None) -> set[int]:
    """The id of `loc` and everything nested beneath it (cycle-guarded)."""
    seen = seen if seen is not None else set()
    if loc.id in seen:
        return seen
    seen.add(loc.id)
    for child in loc.children:
        _descendant_ids(child, seen)
    return seen


@router.get("")
async def locations_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    all_locs = db.query(StorageLocation).order_by(StorageLocation.name).all()
    roots = [loc for loc in all_locs if loc.parent_id is None]
    # Direct warehouse-part count per location.
    counts: dict[int, int] = {}
    for loc in all_locs:
        counts[loc.id] = (
            db.query(Part)
            .filter(
                Part.location_id == loc.id,
                Part.project_id.is_(None),
                Part.device_id.is_(None),
            )
            .count()
        )
    return templates.TemplateResponse(
        "warehouse/locations.html",
        ctx(
            request, db, active="warehouse", whtab="locations",
            roots=roots, all_locs=all_locs, counts=counts,
            msg=request.query_params.get("msg"),
        ),
    )


@router.post("")
async def create_location(
    name: str = Form(...),
    parent_id: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    if not name.strip():
        return RedirectResponse("/warehouse/locations?msg=Name required", status_code=303)
    loc = StorageLocation(
        name=name.strip(),
        parent_id=_fk(parent_id),
        notes=notes.strip() or None,
        code=codes.generate(db, "location"),
    )
    db.add(loc)
    db.commit()
    return RedirectResponse("/warehouse/locations?msg=Location created", status_code=303)


@router.post("/{loc_id}/update")
async def update_location(
    loc_id: int,
    name: str = Form(...),
    parent_id: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    loc = db.get(StorageLocation, loc_id)
    if loc and name.strip():
        new_parent = _fk(parent_id)
        # Guard against making a location its own ancestor (would orphan a subtree).
        if new_parent is None or new_parent not in _descendant_ids(loc):
            loc.parent_id = new_parent
        loc.name = name.strip()
        loc.notes = notes.strip() or None
        if not loc.code:
            loc.code = codes.generate(db, "location")
        db.commit()
    return RedirectResponse("/warehouse/locations?msg=Location saved", status_code=303)


@router.post("/{loc_id}/delete")
async def delete_location(
    loc_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    loc = db.get(StorageLocation, loc_id)
    if loc:
        parent_id = loc.parent_id
        # Re-parent children up one level; move items to the parent location.
        for child in list(loc.children):
            child.parent_id = parent_id
        for part in db.query(Part).filter(Part.location_id == loc_id).all():
            part.location_id = parent_id
        for ps in db.query(PartSet).filter(PartSet.location_id == loc_id).all():
            ps.location_id = parent_id
        db.delete(loc)
        db.commit()
    return RedirectResponse("/warehouse/locations?msg=Location deleted", status_code=303)
