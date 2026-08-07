"""Category management (Settings › Categories).

Categories define the extra, category-specific fields a part can carry (e.g.
CPU → platform AM4/AM5). The field schema is edited client-side and submitted as
a JSON string, which we sanitize before storing.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Category, Part
from ..services import warehouse as wh
from ..templating import ctx, templates

router = APIRouter(prefix="/settings/categories")

# Palette-adjacent hues auto-assigned to new categories (user can recolour).
CATEGORY_COLORS = [
    "#e0779f", "#fb6734", "#5fa0d6", "#46c98b",
    "#f0a23a", "#c98adf", "#ce3737", "#6fb0a0",
]


def _norm_color(value: str) -> str | None:
    value = (value or "").strip()
    if value.startswith("#") and len(value) in (4, 7):
        return value
    return None


@router.get("")
async def categories_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    categories = db.query(Category).order_by(Category.position, Category.name).all()
    counts = {
        c.id: db.query(Part).filter(Part.category_id == c.id).count()
        for c in categories
    }
    import json as _json

    return templates.TemplateResponse(
        "settings_categories.html",
        ctx(
            request, db, active="settings", tab="categories",
            categories=categories, counts=counts,
            field_types=wh.FIELD_TYPES,
            optional_fields_json=_json.dumps(wh.optional_fields(db), ensure_ascii=False),
            msg=request.query_params.get("msg"),
        ),
    )


@router.get("/{cat_id}/fields")
async def category_fields(
    cat_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Field schema for one category — consumed by the warehouse form JS."""
    cat = db.get(Category, cat_id)
    return JSONResponse(cat.fields if cat else [])


@router.post("")
async def create_category(
    name: str = Form(...),
    icon: str = Form(""),
    color: str = Form(""),
    fields_json: str = Form("[]"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/settings/categories?msg=Name required", status_code=303)
    if db.query(Category).filter(Category.name == name).first():
        return RedirectResponse(
            "/settings/categories?msg=Category already exists", status_code=303
        )
    count = db.query(Category).count()
    last = db.query(Category).order_by(Category.position.desc()).first()
    cat = Category(
        name=name,
        icon=(icon.strip() or None),
        color=_norm_color(color) or CATEGORY_COLORS[count % len(CATEGORY_COLORS)],
        position=(last.position + 1) if last else 0,
        fields_json=wh.sanitize_fields(fields_json),
    )
    db.add(cat)
    db.commit()
    return RedirectResponse("/settings/categories?msg=Category created", status_code=303)


@router.post("/{cat_id}/update")
async def update_category(
    cat_id: int,
    name: str = Form(...),
    icon: str = Form(""),
    color: str = Form(""),
    fields_json: str = Form("[]"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    cat = db.get(Category, cat_id)
    if not cat:
        return RedirectResponse("/settings/categories", status_code=303)
    name = name.strip()
    if name:
        clash = (
            db.query(Category)
            .filter(Category.name == name, Category.id != cat_id)
            .first()
        )
        if clash:
            return RedirectResponse(
                "/settings/categories?msg=Category already exists", status_code=303
            )
        cat.name = name
    cat.icon = icon.strip() or None
    cat.color = _norm_color(color) or cat.color
    cat.fields_json = wh.sanitize_fields(fields_json)
    db.commit()
    return RedirectResponse("/settings/categories?msg=Category saved", status_code=303)


@router.post("/{cat_id}/delete")
async def delete_category(
    cat_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    cat = db.get(Category, cat_id)
    if cat:
        # Unlink parts (keep their stored attributes harmlessly orphaned).
        for part in db.query(Part).filter(Part.category_id == cat_id).all():
            part.category_id = None
        db.delete(cat)
        db.commit()
    return RedirectResponse("/settings/categories?msg=Category deleted", status_code=303)
