from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import runtime
from ..auth import require_login
from ..config import get_settings as get_app_settings
from ..db import get_db
from ..models import (
    Expense, OrderInvoice, Part, PartOrigin, Project, ProjectKind, ProjectStatus, WorkSession,
)
from ..services import expenses as exp_service, hub
from ..services.finance import compute_project, global_hourly_rate
from ..services.integrations import invoiceninja, vikunja
from ..services.markdown import export_project_to_file, render_project_markdown
from ..services.uploads import delete_image, save_image_or_error, save_receipt
from ..templating import ctx, templates

app_settings = get_app_settings()

router = APIRouter(prefix="/projects")


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None


@router.get("")
async def list_projects(
    request: Request,
    status: str = "active",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    q = db.query(Project)
    if status == "active":
        q = q.filter(Project.status == ProjectStatus.in_production)
    elif status == "archived":
        q = q.filter(Project.status == ProjectStatus.archived)
    elif status == "sold":
        q = q.filter(Project.status == ProjectStatus.sold)
    projects = q.order_by(Project.created_at.desc()).all()

    rows = [compute_project(db, p) for p in projects]
    return templates.TemplateResponse(
        "projects/list.html",
        ctx(request, db, active="projects", rows=rows, status=status),
    )


@router.post("")
async def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    purchase_price: str = Form(""),
    kind: str = Form("customer"),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    img_url, img_err = save_image_or_error(image, "project")
    price = _parse_float(purchase_price) or 0.0
    project = Project(
        name=name.strip(),
        description=description.strip() or None,
        purchase_price=price,
        kind=ProjectKind(kind) if kind in ProjectKind._value2member_map_ else ProjectKind.customer,
        image_path=img_url,
    )
    db.add(project)
    db.commit()
    # If a receipt for the device purchase was attached, log it as an expense.
    rpath = save_receipt(receipt, "receipt")
    if rpath and price > 0:
        exp_service.create(
            db, amount=price, expense_date=date.today(),
            vendor="", description=f"Device purchase: {project.name}",
            category="Device purchase", project_id=project.id, receipt_path=rpath,
        )
    dest = f"/projects/{project.id}"
    if img_err:
        dest += f"?msg={img_err}"
    return RedirectResponse(dest, status_code=303)


@router.get("/{project_id}")
async def project_detail(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    f = compute_project(db, project)
    warehouse_parts = (
        db.query(Part)
        .filter(Part.project_id.is_(None))
        .order_by(Part.name)
        .all()
    )
    project_invoice = (
        db.query(OrderInvoice)
        .filter(OrderInvoice.project_id == project.id)
        .first()
    )
    in_clients = []
    if invoiceninja.is_enabled() and not project_invoice:
        try:
            in_clients = invoiceninja.list_clients()
        except Exception:  # noqa: BLE001
            in_clients = []
    return templates.TemplateResponse(
        "projects/detail.html",
        ctx(
            request,
            db,
            active="projects",
            f=f,
            project=project,
            warehouse_parts=warehouse_parts,
            global_rate=global_hourly_rate(db),
            today=date.today().isoformat(),
            statuses=list(ProjectStatus),
            kinds=list(ProjectKind),
            in_enabled=invoiceninja.is_enabled(),
            in_url=invoiceninja.base_url(),
            in_clients=in_clients,
            project_invoice=project_invoice,
            project_expenses=db.query(Expense).filter(Expense.project_id == project.id).order_by(Expense.expense_date.desc()).all(),
            shop_sale=db.query(OrderInvoice).filter(
                OrderInvoice.project_id == project.id, OrderInvoice.woo_order_id.isnot(None)
            ).first(),
            woo_url=runtime.get("woo_url").rstrip("/"),
            vikunja_enabled=vikunja.is_enabled(),
            vikunja_url=vikunja.web_url() if vikunja.is_enabled() else "",
        ),
    )


@router.post("/{project_id}/update")
async def update_project(
    project_id: int,
    name: str = Form(...),
    description: str = Form(""),
    status: str = Form("in_production"),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    hourly_rate: str = Form(""),
    vikunja_task_id: str = Form(""),
    kind: str = Form("customer"),
    woo_product_id: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    project.vikunja_task_id = vikunja_task_id.strip() or None
    if kind in ProjectKind._value2member_map_:
        project.kind = ProjectKind(kind)
    project.woo_product_id = int(woo_product_id) if woo_product_id.strip().isdigit() else None
    new_image, img_err = save_image_or_error(image, "project")
    if new_image:
        delete_image(project.image_path)
        project.image_path = new_image
    project.name = name.strip()
    project.description = description.strip() or None
    new_status = ProjectStatus(status)
    if new_status == ProjectStatus.archived and project.status != ProjectStatus.archived:
        project.archived_at = datetime.utcnow()
    project.status = new_status
    project.purchase_price = _parse_float(purchase_price) or 0.0
    project.sale_price = _parse_float(sale_price)
    project.hourly_rate = _parse_float(hourly_rate)
    db.commit()
    dest = f"/projects/{project.id}"
    if img_err:
        dest += f"?msg={img_err}"
    return RedirectResponse(dest, status_code=303)


@router.post("/{project_id}/delete")
async def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if project:
        # Move its parts to the warehouse rather than deleting them.
        for p in list(project.parts):
            p.project_id = None
        db.delete(project)
        db.commit()
    return RedirectResponse("/projects", status_code=303)


# ---- Parts ----

@router.post("/{project_id}/parts")
async def add_part(
    project_id: int,
    name: str = Form(...),
    purchase_price: str = Form(""),
    sale_price: str = Form(""),
    origin: str = Form("purchased"),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    receipt: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    img_url, img_err = save_image_or_error(image, "part")
    pp = _parse_float(purchase_price)
    part = Part(
        name=name.strip(),
        notes=notes.strip() or None,
        project_id=project.id,
        origin=PartOrigin(origin),
        purchase_price=pp,
        sale_price=_parse_float(sale_price) or 0.0,
        image_path=img_url,
    )
    db.add(part)
    db.commit()
    rpath = save_receipt(receipt, "receipt")
    if rpath and pp and pp > 0:
        exp_service.create(
            db, amount=pp, expense_date=date.today(), vendor="",
            description=f"Part: {part.name}", category="Parts",
            project_id=project.id, receipt_path=rpath,
        )
    dest = f"/projects/{project_id}"
    if img_err:
        dest += f"?msg={img_err}"
    return RedirectResponse(dest, status_code=303)


@router.post("/{project_id}/parts/{part_id}/remove")
async def remove_part_to_warehouse(
    project_id: int,
    part_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    if part and part.project_id == project_id:
        # Moving out of a build: it becomes a harvested warehouse part.
        part.project_id = None
        if part.origin == PartOrigin.purchased and not part.purchase_price:
            part.origin = PartOrigin.harvested
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/parts/{part_id}/delete")
async def delete_part(
    project_id: int,
    part_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.get(Part, part_id)
    if part and part.project_id == project_id:
        db.delete(part)
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/parts/install")
async def install_from_warehouse(
    project_id: int,
    part_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Install an existing warehouse part into this project.
    Its stored sale price carries over automatically."""
    part = db.get(Part, part_id)
    if part and part.project_id is None:
        part.project_id = project_id
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ---- Work sessions ----

@router.post("/{project_id}/sessions")
async def add_session(
    project_id: int,
    work_date: str = Form(""),
    hours: str = Form("0"),
    hourly_rate: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    try:
        wd = date.fromisoformat(work_date) if work_date else date.today()
    except ValueError:
        wd = date.today()
    db.add(
        WorkSession(
            project_id=project.id,
            work_date=wd,
            hours=_parse_float(hours) or 0.0,
            hourly_rate=_parse_float(hourly_rate),
            description=description.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/sessions/{session_id}/delete")
async def delete_session(
    project_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    s = db.get(WorkSession, session_id)
    if s and s.project_id == project_id:
        db.delete(s)
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ---- Export ----

@router.get("/{project_id}/export.md")
async def export_download(
    project_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    content = render_project_markdown(db, project)
    from ..services.markdown import _slug

    filename = f"{project.id:04d}-{_slug(project.name)}.md"
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        media_type="text/markdown; charset=utf-8",
    )


@router.post("/{project_id}/export")
async def export_to_vault(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    try:
        path = export_project_to_file(db, project)
        msg = f"Exported to {path}"
    except OSError as e:
        msg = f"Export failed: {e}"
    return RedirectResponse(
        f"/projects/{project_id}?msg={msg}", status_code=303
    )


# ---- Invoicing (InvoiceNinja) ----

@router.post("/{project_id}/invoice")
async def create_project_invoice(
    project_id: int,
    client_id: str = Form(""),
    email: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    company: str = Form(""),
    address1: str = Form(""),
    postal_code: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    address = {
        "address1": address1.strip(), "postal_code": postal_code.strip(),
        "city": city.strip(), "state": state.strip(), "phone": phone.strip(),
    }
    try:
        link = hub.create_invoice_for_project(
            db, project, client_id=client_id.strip(), email=email.strip(),
            first_name=first_name.strip(), last_name=last_name.strip(),
            company=company.strip(), address=address,
        )
        msg = f"Invoice {link.invoice_number or ''} created."
        if link.emailed_at:
            msg += " Sent to customer."
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/projects/{project_id}?msg={msg}", status_code=303)


@router.post("/{project_id}/invoice/send")
async def send_project_invoice(
    project_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    link = (
        db.query(OrderInvoice)
        .filter(OrderInvoice.project_id == project_id)
        .first()
    )
    if not link:
        return RedirectResponse(
            f"/projects/{project_id}?msg=No invoice present", status_code=303
        )
    try:
        hub.send_invoice(db, link)
        msg = f"Invoice {link.invoice_number or ''} sent to customer."
    except Exception as e:  # noqa: BLE001
        msg = f"Error while sending: {e}"
    return RedirectResponse(f"/projects/{project_id}?msg={msg}", status_code=303)
