from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..db import get_db, get_setting, set_setting
from ..models import Part, Project, ProjectStatus
from ..services.finance import compute_stats
from ..services.integrations import invoiceninja, vikunja, woo
from ..services.uploads import delete_image, save_image
from ..templating import ctx, templates

router = APIRouter()

# Canonical widget order + labels.
ALL_WIDGETS = [
    ("welcome", "Greeting"),
    ("finance", "Finances"),
    ("projects", "Active projects"),
    ("warehouse", "Warehouse"),
    ("invoices", "Open invoices"),
    ("orders", "Shop orders"),
    ("tasks", "Tasks"),
    ("quick", "Quick access"),
    ("scan", "Scan"),
    ("logo", "Logo"),
]
DEFAULT_WIDGETS = "welcome:4,finance:2,projects:2,warehouse:1,invoices:1,orders:2,tasks:1,quick:2,scan:1"

# GridStack runs on 12 columns; a widget's stored size 1..4 is a quarter of that.
GRID_COLUMNS = 12
# Cards are a uniform 3 rows so a row packs flush. The hero is the deliberate
# exception: at 2 it reads as a band across the top, not a big empty greeting.
# The height each tile needs for its content, measured in the browser at the
# narrowest width its column reaches. Nothing on the dashboard scrolls and
# nothing is cut off, so this is a floor rather than a suggestion: a saved
# layout is raised to it, and the grid refuses to drag below it.
#   finance             carries a stat row more than the rest
#   quick               six buttons, which fall into three rows once narrow
#   warehouse/invoices  a quarter-width column wraps their stat labels
#   welcome             is a band, and its content is trimmed to fit two
MIN_HEIGHT = {
    "welcome": 2, "finance": 4, "quick": 4, "warehouse": 4, "invoices": 4,
}
MIN_ROWS = 3


def _min_h(key: str) -> int:
    return MIN_HEIGHT.get(key, MIN_ROWS)


def _default_layout(widgets: list[dict]) -> dict:
    """Position every tile, for as long as the user has not arranged them.

    Widths are packed into full rows, and a row that would end short has its
    last tile grown to fill the remainder. Without that the arrangement only
    looks right when the chosen sizes happen to add up to twelve: turn one
    widget off, or on, and the row ends at nine with a hole standing next to
    it, because GridStack floats and never closes a gap by itself.
    """
    rows: list[list[dict]] = []
    row: list[dict] = []
    used = 0
    for w in widgets:
        width = max(1, min(GRID_COLUMNS, w["size"] * 3))
        if used + width > GRID_COLUMNS and row:
            rows.append(row)
            row, used = [], 0
        row.append({"key": w["key"], "w": width, "h": _min_h(w["key"])})
        used += width
    if row:
        rows.append(row)

    out: dict = {}
    y = 0
    for row in rows:
        short = GRID_COLUMNS - sum(t["w"] for t in row)
        if short > 0:
            row[-1]["w"] += short
        x = 0
        for tile in row:
            out[tile["key"]] = {"x": x, "y": y, "w": tile["w"], "h": tile["h"]}
            x += tile["w"]
        y += max(t["h"] for t in row)
    return out


def _enabled_widgets(db: Session) -> list[dict]:
    """Parse the stored 'key:size,...' list into ordered {key, size} dicts."""
    raw = get_setting(db, "dashboard_widgets", DEFAULT_WIDGETS) or DEFAULT_WIDGETS
    valid = {k for k, _ in ALL_WIDGETS}
    out: list[dict] = []
    seen: set[str] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        key, _, size = tok.partition(":")
        key = key.strip()
        if key in valid and key not in seen:
            try:
                sz = max(1, min(4, int(size)))
            except ValueError:
                sz = 1
            out.append({"key": key, "size": sz})
            seen.add(key)
    return out


@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    enabled = _enabled_widgets(db)
    keys = {w["key"] for w in enabled}
    sizes = {w["key"]: w["size"] for w in enabled}
    try:
        layout = json.loads(get_setting(db, "dashboard_layout", "{}") or "{}")
    except (ValueError, TypeError):
        layout = {}
    # A layout saved before a tile grew, or dragged too small, would hide part
    # of what the tile renders. Raise it instead: the arrangement is the user's,
    # the minimum is what the content needs.
    for key, box in layout.items():
        if isinstance(box, dict) and "h" in box:
            try:
                box["h"] = max(int(box["h"]), _min_h(key))
            except (ValueError, TypeError):
                box["h"] = _min_h(key)
    data: dict = {}

    if keys & {"welcome", "finance", "projects", "warehouse"}:
        stats = compute_stats(db)
        data["stats"] = stats

    if "projects" in keys:
        # Same predicate as Stats.active_count — the widget used the legacy
        # in_production status, so the header counted projects the list below
        # it then claimed did not exist.
        data["active_projects"] = (
            db.query(Project)
            .filter(Project.status.in_([ProjectStatus.open, ProjectStatus.in_progress]))
            .order_by(Project.created_at.desc())
            .limit(6)
            .all()
        )

    if "warehouse" in keys:
        wparts = db.query(Part).filter(Part.project_id.is_(None)).all()
        data["warehouse_count"] = len(wparts)
        data["warehouse_value"] = sum((p.sale_price or 0.0) for p in wparts)

    if "invoices" in keys and invoiceninja.is_enabled():
        try:
            data["in_kpis"] = invoiceninja.get_company_totals()
        except Exception:  # noqa: BLE001
            data["in_kpis"] = None

    if "orders" in keys and woo.is_enabled():
        try:
            data["orders"] = woo.list_orders(limit=5)
        except Exception:  # noqa: BLE001
            data["orders"] = None

    if "tasks" in keys and vikunja.is_enabled():
        try:
            data["task_count"] = vikunja.open_task_count()
        except Exception:  # noqa: BLE001
            data["task_count"] = None

    return templates.TemplateResponse(
        "dashboard.html",
        ctx(
            request, db, active="dashboard",
            widgets=enabled, all_widgets=ALL_WIDGETS, sizes=sizes, layout=layout,
            default_layout=_default_layout(enabled),
            min_heights={w["key"]: _min_h(w["key"]) for w in enabled},
            username=user.display_name or user.username, d=data,
            logo_url=get_setting(db, "dashboard_logo", "") or "",
            woo_on=woo.is_enabled(), in_on=invoiceninja.is_enabled(),
            vikunja_on=vikunja.is_enabled(),
        ),
    )


@router.post("/dashboard/widgets")
async def save_widgets(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    form = await request.form()
    # Preserve drag order, keep only checked widgets, with per-widget size.
    order = form.get("order", "")
    chosen = set(form.getlist("widget"))
    ordered_keys = [k.strip() for k in order.split(",") if k.strip()] or [k for k, _ in ALL_WIDGETS]
    result = []
    for k in ordered_keys:
        if k in chosen:
            try:
                size = max(1, min(4, int(form.get(f"size_{k}", "1"))))
            except (ValueError, TypeError):
                size = 1
            result.append(f"{k}:{size}")
    set_setting(db, "dashboard_widgets", ",".join(result))
    return RedirectResponse("/", status_code=303)


@router.post("/dashboard/logo")
async def upload_logo(
    logo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Store a custom logo shown by the 'logo' dashboard widget."""
    url = save_image(logo, "dashlogo")
    if url:
        old = get_setting(db, "dashboard_logo", "")
        if old and old != url:
            delete_image(old)
        set_setting(db, "dashboard_logo", url)
    return RedirectResponse("/", status_code=303)


@router.post("/dashboard/logo/clear")
async def clear_logo(
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    old = get_setting(db, "dashboard_logo", "")
    if old:
        delete_image(old)
    set_setting(db, "dashboard_logo", "")
    return RedirectResponse("/", status_code=303)


@router.post("/dashboard/layout/reset")
async def reset_layout(
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Drop the saved grid so the built-in per-widget default sizes apply again."""
    set_setting(db, "dashboard_layout", "")
    return RedirectResponse("/", status_code=303)


@router.post("/dashboard/layout")
async def save_layout(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Persist the GridStack layout (x/y/w/h per widget) sent as JSON."""
    valid = {k for k, _ in ALL_WIDGETS}
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    clean: dict = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if k in valid and isinstance(v, dict):
                try:
                    clean[k] = {
                        "x": int(v.get("x", 0)), "y": int(v.get("y", 0)),
                        "w": max(1, int(v.get("w", 3))), "h": max(1, int(v.get("h", 2))),
                    }
                except (ValueError, TypeError):
                    continue
    set_setting(db, "dashboard_layout", json.dumps(clean))
    return {"ok": True}
