"""Vikunja integration (task tracking, Kanban view).

Enable via SECONDTRACK_VIKUNJA_ENABLED=1 and provide SECONDTRACK_VIKUNJA_URL
and SECONDTRACK_VIKUNJA_TOKEN (Vikunja → Settings → API Tokens, with read
access to projects/tasks). We surface the subprojects of the configured parent
project (SECONDTRACK_VIKUNJA_PARENT_PROJECT, default "OpenVuture") and their
Kanban boards. Read-only; creating tasks links out to Vikunja.
"""
from __future__ import annotations

import httpx

from ... import runtime


def is_enabled() -> bool:
    return bool(
        runtime.get_bool("vikunja_enabled")
        and runtime.get("vikunja_url")
        and runtime.get("vikunja_token")
    )


def web_url() -> str:
    return runtime.get("vikunja_url").rstrip("/")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=runtime.get("vikunja_url").rstrip("/") + "/api/v1",
        headers={"Authorization": f"Bearer {runtime.get('vikunja_token')}"},
        timeout=20.0,
    )


def _require() -> None:
    if not is_enabled():
        raise RuntimeError("Vikunja integration is disabled")


def list_projects() -> list[dict]:
    _require()
    with _client() as c:
        resp = c.get("/projects")
        resp.raise_for_status()
        return resp.json() or []


def get_subprojects() -> list[dict]:
    """Subprojects of the configured parent (e.g. shop, customers, website)."""
    projects = list_projects()
    parent_name = runtime.get("vikunja_parent").strip().lower()
    parent = next(
        (
            p for p in projects
            if (p.get("title") or "").strip().lower() == parent_name
        ),
        None,
    )
    if not parent:
        # No parent match: return all top-level projects as a fallback.
        return [p for p in projects if not p.get("parent_project_id")]
    pid = parent.get("id")
    subs = [p for p in projects if p.get("parent_project_id") == pid]
    return subs or [parent]


def get_board(project_id: int) -> list[dict]:
    """Return Kanban buckets as [{title, tasks: [...]}], with fallbacks across
    Vikunja versions."""
    _require()
    with _client() as c:
        buckets = None
        # Newer Vikunja: buckets live under a kanban "view".
        try:
            views = c.get(f"/projects/{project_id}/views").json() or []
            kanban = next(
                (v for v in views if str(v.get("view_kind")).lower() in ("kanban", "3")),
                None,
            ) or (views[0] if views else None)
            if kanban:
                r = c.get(f"/projects/{project_id}/views/{kanban['id']}/buckets")
                if r.status_code == 200:
                    buckets = r.json()
        except Exception:  # noqa: BLE001
            buckets = None

        # Older Vikunja: direct buckets endpoint.
        if buckets is None:
            r = c.get(f"/projects/{project_id}/buckets")
            if r.status_code == 200:
                buckets = r.json()

        # Last resort: a flat task list as a single column.
        if buckets is None:
            r = c.get(f"/projects/{project_id}/tasks")
            r.raise_for_status()
            return [{"title": "Aufgaben", "tasks": r.json() or []}]

    out = []
    for b in buckets or []:
        out.append({"title": b.get("title", "—"), "tasks": b.get("tasks") or []})
    return out


def open_task_count() -> int:
    """Total not-done tasks across the surfaced subprojects."""
    _require()
    total = 0
    with _client() as c:
        for sub in get_subprojects():
            try:
                tasks = c.get(f"/projects/{sub['id']}/tasks").json() or []
                total += sum(1 for t in tasks if not t.get("done"))
            except Exception:  # noqa: BLE001
                continue
    return total
