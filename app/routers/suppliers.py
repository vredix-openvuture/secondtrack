"""Supplier management (Warehouse › Suppliers)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Part, Supplier
from ..templating import ctx, templates

router = APIRouter(prefix="/warehouse/suppliers")


def _apply(sup: Supplier, name, contact, email, phone, website, address, account_no, notes):
    sup.name = name.strip()
    sup.contact = contact.strip() or None
    sup.email = email.strip() or None
    sup.phone = phone.strip() or None
    sup.website = website.strip() or None
    sup.address = address.strip() or None
    sup.account_no = account_no.strip() or None
    sup.notes = notes.strip() or None


@router.get("")
async def suppliers_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    counts = {
        s.id: db.query(Part).filter(Part.supplier_id == s.id).count()
        for s in suppliers
    }
    return templates.TemplateResponse(
        "warehouse/suppliers.html",
        ctx(
            request, db, active="warehouse", whtab="suppliers",
            suppliers=suppliers, counts=counts,
            msg=request.query_params.get("msg"),
        ),
    )


@router.post("")
async def create_supplier(
    name: str = Form(...),
    contact: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),
    address: str = Form(""),
    account_no: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    if not name.strip():
        return RedirectResponse("/warehouse/suppliers?msg=Name required", status_code=303)
    sup = Supplier()
    _apply(sup, name, contact, email, phone, website, address, account_no, notes)
    db.add(sup)
    db.commit()
    return RedirectResponse("/warehouse/suppliers?msg=Supplier created", status_code=303)


@router.post("/{sup_id}/update")
async def update_supplier(
    sup_id: int,
    name: str = Form(...),
    contact: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),
    address: str = Form(""),
    account_no: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    sup = db.get(Supplier, sup_id)
    if sup and name.strip():
        _apply(sup, name, contact, email, phone, website, address, account_no, notes)
        db.commit()
    return RedirectResponse("/warehouse/suppliers?msg=Supplier saved", status_code=303)


@router.post("/{sup_id}/delete")
async def delete_supplier(
    sup_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    sup = db.get(Supplier, sup_id)
    if sup:
        for part in db.query(Part).filter(Part.supplier_id == sup_id).all():
            part.supplier_id = None
        db.delete(sup)
        db.commit()
    return RedirectResponse("/warehouse/suppliers?msg=Supplier deleted", status_code=303)
