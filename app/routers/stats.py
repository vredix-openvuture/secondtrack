from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db
from ..services import expenses as exp_service
from ..services.finance import compute_stats, global_hourly_rate
from ..templating import ctx, templates

router = APIRouter(prefix="/stats")


@router.get("")
async def stats_page(
    request: Request,
    period: str = "all",
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    s = compute_stats(db)
    today = date.today()
    if period == "month":
        start, end = today.replace(day=1), today
    elif period == "year":
        start, end = today.replace(month=1, day=1), today
    else:
        period, start, end = "all", None, None
    pl = exp_service.profit_loss(db, start, end)
    return templates.TemplateResponse(
        "stats.html",
        ctx(request, db, active="stats", s=s, rate=global_hourly_rate(db),
            pl=pl, period=period),
    )
