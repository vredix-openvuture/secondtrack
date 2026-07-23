"""Nextcloud integration (WebDAV file storage for invoices/receipts).

Enable via SECONDTRACK_NEXTCLOUD_ENABLED=1 and provide URL, user and an
*app password* (Nextcloud → Settings → Security → Devices & sessions →
create app password). secondtrack only ever *writes* documents it produced,
under SECONDTRACK_NEXTCLOUD_BASE_PATH; it never reads the user's files.

This module is the only place that talks WebDAV to Nextcloud.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from ... import runtime


def is_enabled() -> bool:
    return bool(
        runtime.get_bool("nc_enabled")
        and runtime.get("nc_url")
        and runtime.get("nc_user")
        and runtime.get("nc_pass")
    )


def base_url() -> str:
    return runtime.get("nc_url").rstrip("/")


def _dav_root() -> str:
    # Per-user WebDAV endpoint for file access.
    return f"{base_url()}/remote.php/dav/files/{runtime.get('nc_user')}"


def _auth() -> tuple[str, str]:
    return (runtime.get("nc_user"), runtime.get("nc_pass"))


def _require() -> None:
    if not is_enabled():
        raise RuntimeError("Nextcloud integration is disabled")


def _norm(path: str) -> str:
    """Leading-slash, collapsed, no trailing slash. '/a//b/' -> '/a/b'."""
    return "/" + "/".join(seg for seg in (path or "").split("/") if seg)


def _encode(path: str) -> str:
    """URL-encode each path segment (spaces, umlauts, …) but keep the slashes."""
    return "/".join(quote(seg) for seg in _norm(path).split("/") if seg)


def _url_for(path: str) -> str:
    return f"{_dav_root()}/{_encode(path)}"


def ensure_dir(path: str) -> None:
    """Create a collection and every missing parent (idempotent)."""
    _require()
    segs = [s for s in _norm(path).split("/") if s]
    cur = ""
    with httpx.Client(auth=_auth(), timeout=30.0) as c:
        for seg in segs:
            cur += "/" + seg
            r = c.request("MKCOL", _url_for(cur))
            # 201 = created, 405 = already exists → both are fine.
            if r.status_code not in (201, 405):
                r.raise_for_status()


def put_file(
    remote_path: str, content: bytes, content_type: str = "application/octet-stream"
) -> str:
    """Upload bytes to remote_path (relative to the user's files root), creating
    parent folders as needed. Returns the stored remote path."""
    _require()
    rp = _norm(remote_path)
    parent = rp.rsplit("/", 1)[0]
    if parent and parent != "/":
        ensure_dir(parent)
    with httpx.Client(auth=_auth(), timeout=60.0) as c:
        r = c.put(_url_for(rp), content=content, headers={"Content-Type": content_type})
        r.raise_for_status()
    return rp


def delete_file(remote_path: str) -> None:
    """Delete a file (tolerates 404). Used for cleanup/replacement."""
    _require()
    with httpx.Client(auth=_auth(), timeout=30.0) as c:
        r = c.request("DELETE", _url_for(remote_path))
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()


def move_file(src: str, dst: str) -> bool:
    """WebDAV MOVE src → dst, creating dst's parent folders first. Returns True
    if the file moved (False if the source was already gone / 404)."""
    _require()
    dst_n = _norm(dst)
    parent = dst_n.rsplit("/", 1)[0]
    if parent and parent != "/":
        ensure_dir(parent)
    with httpx.Client(auth=_auth(), timeout=30.0) as c:
        r = c.request(
            "MOVE", _url_for(src),
            headers={"Destination": _url_for(dst_n), "Overwrite": "T"},
        )
        if r.status_code == 404:
            return False
        if r.status_code not in (201, 204):
            r.raise_for_status()
        return True


def test_connection() -> bool:
    """PROPFIND the DAV root to verify URL + credentials."""
    _require()
    with httpx.Client(auth=_auth(), timeout=20.0) as c:
        r = c.request("PROPFIND", _dav_root() + "/", headers={"Depth": "0"})
        return r.status_code in (200, 207)


def web_url(remote_path: str = "") -> str:
    """A browser deep-link into the Nextcloud Files app for a folder path."""
    return f"{base_url()}/index.php/apps/files/?dir={quote(_norm(remote_path))}"
