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
    """The configured parent project *and* its subprojects (shop, customers,
    website, …). The parent is surfaced first: it is a task list of its own and
    its tasks would otherwise be invisible in the Tasks view."""
    projects = list_projects()
    parent = _find_parent(projects)
    if not parent:
        # No parent match: return all top-level projects as a fallback.
        return [p for p in projects if not p.get("parent_project_id")]
    pid = parent.get("id")
    subs = [p for p in projects if p.get("parent_project_id") == pid]
    return [parent, *subs]


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


def get_kanban(project_id: int) -> list[dict]:
    """Kanban columns for one project as [{id, title, tasks:[...]}]. Vikunja's
    kanban buckets are view-specific and embed their tasks, so we use those
    directly; only if none are embedded do we fall back to grouping the flat
    task list by bucket_id."""
    _require()
    default_bid = done_bid = None
    with _client() as c:
        buckets = []
        try:
            views = c.get(f"/projects/{int(project_id)}/views").json() or []
            kanban = next(
                (v for v in views if str(v.get("view_kind")).lower() in ("kanban", "3")),
                None,
            ) or (views[0] if views else None)
            if kanban:
                default_bid = kanban.get("default_bucket_id")
                done_bid = kanban.get("done_bucket_id")
                r = c.get(f"/projects/{int(project_id)}/views/{kanban['id']}/buckets")
                if r.status_code == 200:
                    buckets = r.json() or []
        except Exception:  # noqa: BLE001
            buckets = []
        tasks = c.get(f"/projects/{int(project_id)}/tasks").json() or []

    if not buckets:
        return [{"id": 0, "title": "Tasks", "tasks": tasks}]

    # Preferred: tasks embedded in the view's buckets (some Vikunja versions).
    if sum(len(b.get("tasks") or []) for b in buckets):
        return [
            {"id": b.get("id"), "title": b.get("title", "—"),
             "tasks": b.get("tasks") or [], "done": b.get("id") == done_bid}
            for b in buckets
        ]

    # Vikunja v2.x: buckets don't embed tasks and task.bucket_id is 0. Respect a
    # real bucket_id when present; otherwise put done tasks in the view's done
    # bucket and everything else in its default bucket.
    ids = {b.get("id") for b in buckets}
    fallback_bid = default_bid if default_bid in ids else next(iter(ids), None)
    done_col = done_bid if done_bid in ids else None
    by_bucket: dict = {b.get("id"): [] for b in buckets}
    for t in tasks:
        bid = t.get("bucket_id") or 0
        if bid not in ids:
            bid = done_col if (t.get("done") and done_col) else fallback_bid
        by_bucket.setdefault(bid, []).append(t)
    return [
        {"id": b.get("id"), "title": b.get("title", "—"),
         "tasks": by_bucket.get(b.get("id"), []), "done": b.get("id") == done_bid}
        for b in buckets
    ]


def move_task_to_bucket(project_id: int, bucket_id: int, task_id: int) -> None:
    """Move a task into a kanban bucket of the project's kanban view, and keep
    its done-status in sync with the bucket (the view's done bucket ⇒ done).
    Because we can't read view-specific bucket assignments back, syncing the
    done flag is what makes the To-Do ↔ Done placement survive a reload."""
    _require()
    with _client() as c:
        views = c.get(f"/projects/{int(project_id)}/views").json() or []
        kb = next(
            (v for v in views if str(v.get("view_kind")).lower() in ("kanban", "3")),
            None,
        ) or (views[0] if views else None)
        if not kb:
            raise RuntimeError("no kanban view")
        vid = kb["id"]
        done_bid = kb.get("done_bucket_id")
        r = c.post(
            f"/projects/{int(project_id)}/views/{vid}/buckets/{int(bucket_id)}/tasks",
            json={"task_id": int(task_id)},
        )
        r.raise_for_status()
        want_done = done_bid is not None and int(bucket_id) == int(done_bid)
        task = c.get(f"/tasks/{int(task_id)}").json() or {}
        if bool(task.get("done")) != want_done:
            task["done"] = want_done
            c.post(f"/tasks/{int(task_id)}", json=task)


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


def nest_tasks(tasks: list[dict]) -> list[dict]:
    """Turn a flat task list into a parent→child tree via Vikunja's
    related_tasks. Each task gains a '_children' list; only roots are returned.
    Nesting stays within the given set (a subtask whose parent isn't in the set
    — e.g. a done or cross-project parent — is treated as a root)."""
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        t["_children"] = []
    roots = []
    for t in tasks:
        parents = (t.get("related_tasks") or {}).get("parenttask") or []
        parent = next(
            (by_id[p["id"]] for p in parents if p.get("id") in by_id and p.get("id") != t["id"]),
            None,
        )
        if parent is not None:
            parent["_children"].append(t)
        else:
            roots.append(t)
    return roots or tasks  # fall back to flat if a cycle ate every root


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
                    "count": len(open_tasks),
                    "tasks": nest_tasks(open_tasks),
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


def update_task(task_id: int, fields: dict) -> dict:
    """Merge `fields` (title, description, priority, due_date, …) into the task
    and save it. Vikunja updates a task via POST with the whole object, so we
    fetch-merge-post to avoid clobbering fields we're not editing."""
    _require()
    with _client() as c:
        task = c.get(f"/tasks/{int(task_id)}").json() or {}
        task.update(fields)
        r = c.post(f"/tasks/{int(task_id)}", json=task)
        r.raise_for_status()
        return r.json()


def list_labels() -> list[dict]:
    """All labels available to the user (for the tag picker)."""
    _require()
    with _client() as c:
        r = c.get("/labels")
        r.raise_for_status()
        return r.json() or []


def create_label(title: str, hex_color: str = "") -> dict:
    """Create a new label and return it (Vikunja creates via PUT /labels)."""
    _require()
    body: dict = {"title": title.strip()}
    if hex_color:
        body["hex_color"] = hex_color.lstrip("#")
    with _client() as c:
        r = c.put("/labels", json=body)
        r.raise_for_status()
        return r.json()


def add_label(task_id: int, label_id: int) -> None:
    """Attach an existing label to a task (PUT /tasks/{id}/labels)."""
    _require()
    with _client() as c:
        r = c.put(f"/tasks/{int(task_id)}/labels", json={"label_id": int(label_id)})
        r.raise_for_status()


def remove_label(task_id: int, label_id: int) -> None:
    """Detach a label from a task (DELETE /tasks/{id}/labels/{label_id})."""
    _require()
    with _client() as c:
        r = c.delete(f"/tasks/{int(task_id)}/labels/{int(label_id)}")
        r.raise_for_status()


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
