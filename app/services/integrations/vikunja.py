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


def _norm(title: str) -> str:
    """Normalize a project title for matching: drop emoji/symbols/spaces,
    lowercase. So '🌞 OpenVuture' matches the configured 'OpenVuture'."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def _find_parent(projects: list[dict]) -> dict | None:
    """The configured parent project, matched tolerantly (ignores emoji prefixes)."""
    target = _norm(runtime.get("vikunja_parent"))
    if not target:
        return None
    return next((p for p in projects if _norm(p.get("title") or "") == target), None)


def list_projects() -> list[dict]:
    _require()
    with _client() as c:
        resp = c.get("/projects")
        resp.raise_for_status()
        return resp.json() or []


def get_subprojects() -> list[dict]:
    """Subprojects of the configured parent (e.g. shop, customers, website)."""
    projects = list_projects()
    parent = _find_parent(projects)
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


def open_tasks_by_subproject() -> list[dict]:
    """Open (not-done) tasks grouped per configured subproject, as
    [{title, tasks:[...]}]. Only subprojects that actually have open tasks are
    returned, so the aggregated Tasks view surfaces all open work at once
    instead of just the first (possibly empty) subproject."""
    _require()
    out = []
    with _client() as c:
        for sub in get_subprojects():
            try:
                tasks = c.get(f"/projects/{sub['id']}/tasks").json() or []
            except Exception:  # noqa: BLE001
                continue
            open_tasks = [t for t in tasks if not t.get("done")]
            if open_tasks:
                out.append({
                    "title": sub.get("title", "—"),
                    "id": sub.get("id"),
                    "has_bg": bool(sub.get("background_blur_hash")),
                    "tasks": open_tasks,
                })
    return out


def get_project_background(project_id: int) -> tuple[bytes, str] | None:
    """A Vikunja project's background image as (bytes, content_type), or None."""
    if not is_enabled():
        return None
    try:
        with _client() as c:
            r = c.get(f"/projects/{int(project_id)}/background")
            if r.status_code == 200 and r.content:
                return r.content, r.headers.get("content-type", "image/jpeg")
    except Exception:  # noqa: BLE001
        return None
    return None


def open_tasks_for(project_id: int) -> list[dict]:
    """Open (not-done) tasks of a single project — reliable across Vikunja
    versions (the kanban buckets endpoint doesn't always embed tasks)."""
    _require()
    with _client() as c:
        tasks = c.get(f"/projects/{int(project_id)}/tasks").json() or []
    return [t for t in tasks if not t.get("done")]


# ── Write path (create tasks/projects; see ARCHITEKTUR-SOLL.md) ──────────────
# Vikunja creates via PUT: `PUT /projects` and `PUT /projects/{id}/tasks`.

def _rfc3339(day: str) -> str:
    """Accept 'YYYY-MM-DD' or a full ISO string; return an RFC3339 timestamp
    (Vikunja stores due_date as a timestamp, midnight UTC for a bare date)."""
    day = (day or "").strip()
    if not day:
        return ""
    if "T" in day:
        return day if day.endswith("Z") or "+" in day else day + "Z"
    return f"{day}T00:00:00Z"


def create_project(
    title: str, parent_project_id: int | None = None, description: str = ""
) -> dict:
    """Create a Vikunja project (optionally as a subproject) and return it."""
    _require()
    body: dict = {"title": title.strip()}
    if description:
        body["description"] = description
    if parent_project_id:
        body["parent_project_id"] = int(parent_project_id)
    with _client() as c:
        resp = c.put("/projects", json=body)
        resp.raise_for_status()
        return resp.json()


def create_task(
    project_id: int, title: str, description: str = "", due_date: str = ""
) -> dict:
    """Create a task in the given Vikunja project and return it."""
    _require()
    body: dict = {"title": title.strip()}
    if description:
        body["description"] = description
    due = _rfc3339(due_date)
    if due:
        body["due_date"] = due
    with _client() as c:
        resp = c.put(f"/projects/{int(project_id)}/tasks", json=body)
        resp.raise_for_status()
        return resp.json()


def get_task(task_id: int) -> dict:
    """Full task object from Vikunja (title, description, due_date, priority,
    labels, assignees, timestamps, …)."""
    _require()
    with _client() as c:
        r = c.get(f"/tasks/{int(task_id)}")
        r.raise_for_status()
        return r.json() or {}


def toggle_task_done(task_id: int) -> dict:
    """Flip a task's done state (fetch, invert, POST the whole task back —
    Vikunja updates tasks via POST /tasks/{id})."""
    _require()
    with _client() as c:
        task = c.get(f"/tasks/{int(task_id)}").json() or {}
        task["done"] = not task.get("done")
        r = c.post(f"/tasks/{int(task_id)}", json=task)
        r.raise_for_status()
        return r.json()


def parent_project_id() -> int | None:
    """id of the configured parent project (SECONDTRACK_VIKUNJA_PARENT_PROJECT)."""
    _require()
    parent = _find_parent(list_projects())
    return parent.get("id") if parent else None


def find_or_create_subproject(name: str) -> int:
    """Return the id of subproject `name` under the configured parent, creating
    it if it doesn't exist yet."""
    _require()
    name_l = name.strip().lower()
    for p in list_projects():
        if (p.get("title") or "").strip().lower() == name_l:
            return int(p["id"])
    created = create_project(name, parent_project_id=parent_project_id())
    return int(created["id"])
