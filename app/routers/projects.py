from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import runtime
from ..auth import require_login
from ..config import get_settings as get_app_settings
from ..db import get_db, new_project_number
from ..models import (
    Customer, CustomerKind, Expense, OrderInvoice, Part, PartSet,
    PartOrigin, Project, ProjectStatus, ProjectType, Report,
    WorkSession,
)
from ..services import hub
from ..services import warehouse as wh
from ..services.finance import compute_project, global_hourly_rate
from ..services.integrations import invoiceninja, vikunja
from ..services.markdown import export_project_to_file, render_project_markdown
from ..services.uploads import delete_image, save_image_or_error
from ..templating import ctx, templates

app_settings = get_app_settings()

router = APIRouter(prefix="/projects")

# New container-lifecycle statuses offered in the UI (legacy values are only
# kept in the enum for the transition and never shown as choices).
NEW_STATUSES = [
    ProjectStatus.open,
    ProjectStatus.in_progress,
    ProjectStatus.done,
    ProjectStatus.invoiced,
]




def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None


def _resolve_type(
    db: Session, type_id: str = "", new_name: str = "", new_shop: str = ""
) -> int | None:
    """A project type id: an existing one, or a newly created one. Same shape as
    _resolve_customer, so the form can offer "+ New" without a nested form."""
    name = (new_name or "").strip()
    if name:
        existing = db.query(ProjectType).filter(ProjectType.name == name).first()
        if existing:
            return existing.id
        last = db.query(ProjectType).order_by(ProjectType.position.desc()).first()
        t = ProjectType(
            name=name,
            shop_stock=new_shop.strip().lower() in ("1", "on", "true", "yes"),
            position=(last.position + 1) if last else 0,
        )
        db.add(t)
        db.commit()
        return t.id
    tid = (type_id or "").strip()
    return int(tid) if tid.isdigit() else None


def _resolve_customer(
    db: Session,
    customer_id: str = "",
    new_name: str = "",
    kind: str = "internal",
    email: str = "",
    company: str = "",
) -> int | None:
    """Return a Customer id for a project: an existing one (customer_id), or a
    newly created one (new_name). An 'invoiceninja' customer also gets/creates a
    matching InvoiceNinja client so invoices can be raised against it."""
    cid = (customer_id or "").strip()
    # Picked an existing InvoiceNinja client (value "in:<id>") → find/create the
    # matching secondtrack customer linked to it.
    if cid.startswith("in:"):
        in_id = cid[3:].strip()
        if in_id:
            existing = (
                db.query(Customer)
                .filter(Customer.invoiceninja_client_id == in_id)
                .first()
            )
            if existing:
                return existing.id
            name = in_id
            try:
                for c in invoiceninja.list_clients():
                    if str(c.get("id")) == in_id:
                        name = c.get("name") or in_id
                        break
            except Exception:  # noqa: BLE001
                pass
            cust = Customer(
                name=name, kind=CustomerKind.invoiceninja,
                invoiceninja_client_id=in_id,
            )
            db.add(cust)
            db.commit()
            return cust.id
    if cid.isdigit():
        return int(cid)
    if not new_name.strip():
        return None
    k = CustomerKind(kind) if kind in CustomerKind._value2member_map_ else CustomerKind.internal
    inv_client_id = None
    if k == CustomerKind.invoiceninja and invoiceninja.is_enabled():
        try:
            inv_client_id = invoiceninja.find_or_create_client(
                email=email.strip(), company=company.strip(),
                first_name=new_name.strip(),
            )
        except Exception:  # noqa: BLE001
            inv_client_id = None
    cust = Customer(
        name=new_name.strip(), kind=k,
        email=email.strip() or None, company=company.strip() or None,
        invoiceninja_client_id=inv_client_id,
    )
    db.add(cust)
    db.commit()
    return cust.id


@router.get("")
async def list_projects(
    request: Request,
    status: str = "active",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    q = db.query(Project)
    if status == "active":
        q = q.filter(Project.status.in_([ProjectStatus.open, ProjectStatus.in_progress]))
    elif status == "done":
        q = q.filter(Project.status == ProjectStatus.done)
    elif status == "invoiced":
        q = q.filter(Project.status == ProjectStatus.invoiced)
    projects = q.order_by(Project.created_at.desc()).all()

    rows = [compute_project(db, p) for p in projects]
    customers = db.query(Customer).order_by(Customer.name).all()
    in_clients = []
    if invoiceninja.is_enabled():
        try:
            in_clients = invoiceninja.list_clients()
        except Exception:  # noqa: BLE001
            in_clients = []
    return templates.TemplateResponse(
        "projects/list.html",
        ctx(request, db, active="projects", rows=rows, status=status,
            customers=customers, in_clients=in_clients,
            project_types=db.query(ProjectType).order_by(ProjectType.position, ProjectType.name).all()),
    )


@router.post("")
async def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    type_id: str = Form(""),
    new_type_name: str = Form(""),
    new_type_shop: str = Form(""),
    customer_id: str = Form(""),
    new_customer_name: str = Form(""),
    customer_kind: str = Form("internal"),
    customer_email: str = Form(""),
    customer_company: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    img_url, img_err = save_image_or_error(image, "project")
    cust_id = _resolve_customer(
        db, customer_id, new_customer_name, customer_kind,
        customer_email, customer_company,
    )
    project = Project(
        name=name.strip(),
        title=name.strip(),
        number=new_project_number(db),
        customer_id=cust_id,
        description=description.strip() or None,
        status=ProjectStatus.open,
        type_id=_resolve_type(db, type_id, new_type_name, new_type_shop),
        image_path=img_url,
    )
    db.add(project)
    db.commit()
    # A project starts empty — its items are picked from the warehouse.
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
    # Loose shelf parts only — a part inside a set is picked via that set.
    warehouse_parts = (
        db.query(Part)
        .filter(Part.project_id.is_(None), Part.set_id.is_(None))
        .order_by(Part.name)
        .all()
    )
    project_invoice = (
        db.query(OrderInvoice)
        .filter(OrderInvoice.project_id == project.id)
        .first()
    )
    in_clients = []
    if invoiceninja.is_enabled():
        try:
            in_clients = invoiceninja.list_clients()
        except Exception:  # noqa: BLE001
            in_clients = []
    # Pickable from the shelf: anything not already on a project.
    assignable_items = [
        {"value": f"set:{ps.id}", "label": f"{ps.name} · {ps.code or ''}", "sale": ps.sale_price}
        for ps in db.query(PartSet).filter(PartSet.project_id.is_(None))
        .order_by(PartSet.name).all()
    ] + [
        {"value": f"part:{p.id}", "label": f"{p.name} · {p.code or ''}",
         "sale": p.sale_price, "stock": p.quantity or 1}
        for p in warehouse_parts
    ]
    reports = (
        db.query(Report)
        .filter(Report.project_id == project.id)
        .order_by(Report.created_at.desc())
        .all()
    )
    customers = db.query(Customer).order_by(Customer.name).all()
    return templates.TemplateResponse(
        "projects/detail.html",
        ctx(
            request,
            db,
            active="projects",
            f=f,
            project=project,
            items=f.items,  # same list the calculation uses
            assignable_items=assignable_items,
            reports=reports,
            customers=customers,
            global_rate=global_hourly_rate(db),
            today=date.today().isoformat(),
            statuses=NEW_STATUSES,
            project_types=db.query(ProjectType).order_by(ProjectType.position, ProjectType.name).all(),
            in_enabled=invoiceninja.is_enabled(),
            in_url=invoiceninja.base_url(),
            in_clients=in_clients,
            project_invoice=project_invoice,
            project_expenses=db.query(Expense).filter(Expense.project_id == project.id).order_by(Expense.expense_date.desc()).all(),
            # Expenses not yet booked on any project, offered for linking.
            assignable_expenses=db.query(Expense).filter(
                Expense.project_id.is_(None)
            ).order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(100).all(),
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
    status: str = Form("open"),
    hourly_rate: str = Form(""),
    vikunja_task_id: str = Form(""),
    type_id: str = Form(""),
    new_type_name: str = Form(""),
    new_type_shop: str = Form(""),
    customer_id: str = Form(""),
    new_customer_name: str = Form(""),
    customer_kind: str = Form("internal"),
    customer_email: str = Form(""),
    customer_company: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if not project:
        return RedirectResponse("/projects", status_code=303)
    project.vikunja_task_id = vikunja_task_id.strip() or None
    # Only touch the type when the form actually carried one, so an untouched
    # dropdown cannot clear it.
    resolved_type = _resolve_type(db, type_id, new_type_name, new_type_shop)
    if resolved_type is not None:
        project.type_id = resolved_type
    new_image, img_err = save_image_or_error(image, "project")
    if new_image:
        delete_image(project.image_path)
        project.image_path = new_image
    project.name = name.strip()
    project.title = name.strip()
    project.description = description.strip() or None
    new_status = ProjectStatus(status)
    if new_status == ProjectStatus.done and project.status != ProjectStatus.done:
        project.archived_at = datetime.utcnow()
    project.status = new_status
    project.hourly_rate = _parse_float(hourly_rate)
    # Customer: assign an existing one or create a new one (only when provided,
    # so an untouched dropdown leaves the current customer in place).
    _cid = customer_id.strip()
    if new_customer_name.strip() or _cid.isdigit() or _cid.startswith("in:"):
        project.customer_id = _resolve_customer(
            db, customer_id, new_customer_name, customer_kind,
            customer_email, customer_company,
        )
    elif _cid == "none":
        project.customer_id = None
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
        # Its items return to the warehouse rather than being deleted with it.
        # Sets must be released too — both their project links are real foreign
        # keys, so leaving them set makes the delete fail.
        for p in list(project.parts):
            p.project_id = None
            p.device_id = None
        for ps in db.query(PartSet).filter(
            (PartSet.project_id == project.id)
            | (PartSet.source_project_id == project.id)
        ).all():
            ps.project_id = None
            ps.source_project_id = None
        # Expenses booked on the project keep their receipt but lose the link.
        for e in db.query(Expense).filter(Expense.project_id == project.id).all():
            e.project_id = None
            e.bucket = "warehouse"
        db.delete(project)
        db.commit()
    return RedirectResponse("/projects", status_code=303)


# ---- Reports (Markdown) ----

@router.post("/{project_id}/reports")
async def add_report(
    project_id: int,
    title: str = Form(""),
    body_md: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    project = db.get(Project, project_id)
    if project:
        db.add(Report(
            project_id=project.id,
            title=title.strip() or "Report",
            body_md=body_md.strip() or None,
        ))
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/reports/{report_id}/update")
async def update_report(
    project_id: int,
    report_id: int,
    title: str = Form(""),
    body_md: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    report = db.get(Report, report_id)
    if report and report.project_id == project_id:
        report.title = title.strip() or "Report"
        report.body_md = body_md.strip() or None
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/reports/{report_id}/delete")
async def delete_report(
    project_id: int,
    report_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    report = db.get(Report, report_id)
    if report and report.project_id == project_id:
        db.delete(report)
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ---- Parts ----

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
        part.device_id = None
        if part.origin == PartOrigin.purchased and not part.purchase_price:
            part.origin = PartOrigin.harvested
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/items/part/{part_id}/qty")
async def set_item_quantity(
    project_id: int,
    part_id: int,
    qty: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Change how many units of an item are booked on this project. The rest
    goes back on the shelf — a project takes a share of the stock, not the row."""
    part = db.get(Part, part_id)
    if part and part.project_id == project_id and qty.strip().lstrip("-").isdigit():
        wh.set_booked_units(db, part, int(qty))
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/items/assign")
async def assign_item(
    project_id: int,
    item: str = Form(...),
    qty: str = Form("1"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Assign an existing warehouse object — a part, a lot, a WIP build or a
    finished good — to this project. Nothing is created here: items are made in
    the warehouse and picked from it.

    The purchase expense follows the item so the project shows what it actually
    cost. It stays listed under Expenses either way; the link is traceability,
    not a move. `item` is "part:<id>" or "set:<id>".
    """
    kind, _, raw = item.partition(":")
    if not raw.isdigit():
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    oid, shared_receipt = int(raw), False

    if kind == "part":
        part = db.get(Part, oid)
        if part and part.project_id is None:
            want = int(qty) if qty.strip().isdigit() else 1
            _, shared_receipt = wh.assign_units(db, part, want, project_id)
            db.commit()
    elif kind == "set":
        ps = db.get(PartSet, oid)
        if ps and ps.project_id is None:
            ps.project_id = project_id
            # Members come along — otherwise their cost stays in the warehouse.
            for p in db.query(Part).filter(
                Part.set_id == ps.id, Part.project_id.is_(None)
            ).all():
                p.project_id = project_id
            if ps.expense_id:
                exp = db.get(Expense, ps.expense_id)
                if exp and exp.project_id is None:
                    exp.project_id = project_id
                    exp.bucket = "project"
            db.commit()

    if shared_receipt:
        return RedirectResponse(
            f"/projects/{project_id}?msg=Zugewiesen — der Beleg deckt mehrere Objekte ab und bleibt im Lager",
            status_code=303,
        )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/items/set/{set_id}/release")
async def release_set(
    project_id: int,
    set_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Send a set back to the warehouse, taking its member parts with it."""
    ps = db.get(PartSet, set_id)
    if ps and ps.project_id == project_id:
        ps.project_id = None
        for p in db.query(Part).filter(
            Part.set_id == ps.id, Part.project_id == project_id
        ).all():
            p.project_id = None
            p.device_id = None
        db.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/expenses/assign")
async def assign_expense(
    project_id: int,
    expense_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Book an existing expense onto this project instead of creating a new one."""
    exp = db.get(Expense, expense_id)
    if exp and exp.project_id != project_id:
        exp.project_id = project_id
        exp.bucket = "project"
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


@router.post("/{project_id}/sessions/{session_id}/update")
async def update_session(
    project_id: int,
    session_id: int,
    work_date: str = Form(""),
    hours: str = Form(""),
    hourly_rate: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Correct a logged session after the fact — hours get mistyped and the day
    is often only written up later."""
    s = db.get(WorkSession, session_id)
    if s and s.project_id == project_id:
        try:
            s.work_date = date.fromisoformat(work_date) if work_date else s.work_date
        except ValueError:
            pass
        hrs = _parse_float(hours)
        if hrs is not None:
            s.hours = hrs
        # An empty rate means "use the project rate", so it must be clearable.
        s.hourly_rate = _parse_float(hourly_rate)
        s.description = description.strip() or None
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
