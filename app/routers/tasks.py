from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..models import Project
from ..services.integrations import vikunja
from ..templating import ctx, templates

router = APIRouter(prefix="/tasks")


@router.get("")
async def tasks_page(
    request: Request,
    project: int | None = None,
    view: str = "list",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    enabled = vikunja.is_enabled()
    subprojects: list[dict] = []
    board: list[dict] = []
    kanban_cols: list[dict] = []
    selected = None
    error = None

    if enabled:
        try:
            subprojects = vikunja.get_subprojects()
            if project:
                ids = [s["id"] for s in subprojects]
                selected = project if project in ids else (ids[0] if ids else None)
                if selected:
                    sub = next((s for s in subprojects if s["id"] == selected), None)
                    # Flat open-task list for this subproject (buckets don't
                    # reliably embed tasks in this Vikunja version), nested by
                    # parent/subtask relations.
                    open_ = vikunja.open_tasks_for(selected)
                    board = [{
                        "title": sub.get("title", "—") if sub else "—",
                        "id": selected,
                        "has_bg": bool(sub.get("background_blur_hash")) if sub else False,
                        "count": len(open_),
                        "tasks": vikunja.nest_tasks(open_),
                    }]
                    # A per-project Kanban board (columns = Vikunja buckets).
                    if view == "kanban":
                        kanban_cols = vikunja.get_kanban(selected)
            else:
                # Default "All open": aggregate open tasks across all subprojects
                # (the old behaviour showed only the first subproject, which was
                # often empty even while other subprojects had open tasks).
                selected = None
                board = vikunja.open_tasks_by_subproject()
        except Exception as e:  # noqa: BLE001
            error = str(e)

    return templates.TemplateResponse(
        "tasks.html",
        ctx(
            request, db, active="tasks",
            enabled=enabled, subprojects=subprojects, board=board,
            kanban_cols=kanban_cols, view=view,
            selected=selected, error=error, vikunja_url=vikunja.web_url() if enabled else "",
        ),
    )


@router.post("/{task_id}/toggle")
async def toggle_task(
    task_id: int,
    next: str = Form("/tasks"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Check/uncheck a task's done state directly from secondtrack."""
    if vikunja.is_enabled():
        try:
            vikunja.toggle_task_done(task_id)
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(next or "/tasks", status_code=303)


def _detail_url(task_id: int, back: str) -> str:
    return f"/tasks/{task_id}?back={quote(back or '/tasks', safe='')}"


@router.post("/{task_id}/update")
async def update_task(
    task_id: int,
    title: str = Form(""),
    description: str = Form(""),
    priority: str = Form(""),
    due_date: str = Form(""),
    back: str = Form("/tasks"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Save the editable task fields (title, description, priority, due date)."""
    if vikunja.is_enabled():
        fields: dict = {"description": description}
        if title.strip():
            fields["title"] = title.strip()
        try:
            fields["priority"] = int(priority)
        except (TypeError, ValueError):
            pass
        d = due_date.strip()
        # empty clears the due date (Vikunja's "unset" is the zero timestamp)
        fields["due_date"] = vikunja._rfc3339(d) if d else "0001-01-01T00:00:00Z"
        try:
            vikunja.update_task(task_id, fields)
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(_detail_url(task_id, back), status_code=303)


@router.post("/{task_id}/labels/add")
async def add_task_label(
    task_id: int,
    label: str = Form(""),
    back: str = Form("/tasks"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """One field: match the typed name to an existing label (case-insensitive)
    or create a new label with that name, then attach it to the task."""
    name = label.strip()
    if vikunja.is_enabled() and name:
        try:
            existing = {
                (l.get("title") or "").strip().lower(): l["id"]
                for l in vikunja.list_labels()
            }
            lid = existing.get(name.lower())
            if lid is None:
                lid = vikunja.create_label(name)["id"]
            vikunja.add_label(task_id, int(lid))
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(_detail_url(task_id, back), status_code=303)


@router.post("/{task_id}/labels/{label_id}/remove")
async def remove_task_label(
    task_id: int,
    label_id: int,
    back: str = Form("/tasks"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Detach a label from the task."""
    if vikunja.is_enabled():
        try:
            vikunja.remove_label(task_id, label_id)
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(_detail_url(task_id, back), status_code=303)


@router.post("/{task_id}/assign")
async def assign_task_project(
    task_id: int,
    project_id: str = Form(""),
    back: str = Form("/tasks"),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Link this Vikunja task to a SecondTrack project (stored on the project's
    vikunja_task_id). Empty project_id clears the link."""
    tid = str(task_id)
    # A task belongs to at most one project — clear any prior link first.
    for p in db.query(Project).filter(Project.vikunja_task_id == tid).all():
        p.vikunja_task_id = None
    if project_id.strip():
        proj = db.get(Project, int(project_id))
        if proj:
            proj.vikunja_task_id = tid
    db.commit()
    return RedirectResponse(_detail_url(task_id, back), status_code=303)


@router.post("/{task_id}/bucket")
async def move_task_bucket(
    task_id: int,
    project_id: int = Form(...),
    bucket_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Drag & drop: move a task into a kanban bucket (AJAX)."""
    if vikunja.is_enabled():
        try:
            vikunja.move_task_to_bucket(project_id, bucket_id, task_id)
        except Exception:  # noqa: BLE001
            return Response(status_code=500)
    return Response(status_code=204)


@router.get("/project/{project_id}/background")
async def project_background(
    project_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Proxy a Vikunja project's background image (Vikunja token stays server-side)."""
    if vikunja.is_enabled():
        res = vikunja.get_project_background(project_id)
        if res:
            content, ctype = res
            return Response(content=content, media_type=ctype,
                            headers={"Cache-Control": "max-age=3600"})
    return Response(status_code=404)


@router.get("/{task_id}")
async def task_detail(
    task_id: int,
    request: Request,
    back: str = "/tasks",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Full task detail (everything Vikunja exposes for the task)."""
    enabled = vikunja.is_enabled()
    task, error, labels = None, None, []
    if enabled:
        try:
            task = vikunja.get_task(task_id)
        except Exception as e:  # noqa: BLE001
            error = str(e)
        try:
            labels = vikunja.list_labels()
        except Exception:  # noqa: BLE001
            labels = []
    # SecondTrack projects, for assigning this task to one.
    projects = db.query(Project).order_by(Project.name).all()
    linked_project = (
        db.query(Project).filter(Project.vikunja_task_id == str(task_id)).first()
    )
    return templates.TemplateResponse(
        "tasks_detail.html",
        ctx(
            request, db, active="tasks",
            enabled=enabled, task=task, error=error, back=back, labels=labels,
            projects=projects, linked_project=linked_project,
            vikunja_url=vikunja.web_url() if enabled else "",
        ),
    )
