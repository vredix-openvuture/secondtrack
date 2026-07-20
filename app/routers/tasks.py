from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..services.integrations import vikunja
from ..templating import ctx, templates

router = APIRouter(prefix="/tasks")


@router.get("")
async def tasks_page(
    request: Request,
    project: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    enabled = vikunja.is_enabled()
    subprojects: list[dict] = []
    board: list[dict] = []
    selected = None
    error = None

    if enabled:
        try:
            subprojects = vikunja.get_subprojects()
            if subprojects:
                ids = [s["id"] for s in subprojects]
                selected = project if project in ids else ids[0]
                board = vikunja.get_board(selected)
        except Exception as e:  # noqa: BLE001
            error = str(e)

    return templates.TemplateResponse(
        "tasks.html",
        ctx(
            request, db, active="tasks",
            enabled=enabled, subprojects=subprojects, board=board,
            selected=selected, error=error, vikunja_url=vikunja.web_url() if enabled else "",
        ),
    )
