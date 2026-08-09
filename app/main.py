from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import _RedirectToLogin
from .config import get_settings
from .db import init_db
from .routers import (
    auth,
    categories,
    dashboard,
    expenses as expenses_router,
    hub,
    locations,
    projects,
    scan,
    settings as settings_router,
    stats,
    suppliers,
    tasks,
    warehouse,
    webhooks,
)

settings = get_settings()


async def _email_loop():
    import asyncio

    from . import runtime
    from .db import SessionLocal
    from .services import emails

    while True:
        try:
            await asyncio.sleep(60 * 60 * 24)  # daily
            if runtime.get_bool("email_auto"):
                with SessionLocal() as db:
                    emails.process_due(db)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            pass


async def _order_poll_loop():
    import asyncio

    from . import runtime
    from .db import SessionLocal
    from .services import hub

    while True:
        try:
            interval = max(1, runtime.get_int("woo_poll_interval", 5))
            await asyncio.sleep(interval * 60)
            if runtime.get_bool("woo_poll_enabled"):
                with SessionLocal() as db:
                    hub.poll_orders(db)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            pass


async def _nc_archive_loop():
    import asyncio

    from . import runtime
    from .db import SessionLocal
    from .services import hub
    from .services.integrations import nextcloud

    while True:
        try:
            await asyncio.sleep(60 * 15)  # every 15 minutes
            if runtime.get_bool("nc_auto_archive") and nextcloud.is_enabled():
                with SessionLocal() as db:
                    hub.archive_paid_invoices(db)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    init_db()
    from . import runtime
    from .db import SessionLocal

    with SessionLocal() as db:
        runtime.load(db)
    tasks = [
        asyncio.create_task(_email_loop()),
        asyncio.create_task(_order_poll_loop()),
        asyncio.create_task(_nc_archive_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="secondtrack", lifespan=lifespan)

# User uploads (project/part images, wallpaper).
os.makedirs(settings.upload_dir, exist_ok=True)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.cookie_secure,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Served from the root, not /static, because a worker can only control
    paths at or below its own URL — from /static/sw.js it would see nothing.
    No login required: the browser fetches it before any session exists.
    The asset version is stamped in so a deploy invalidates the old cache."""
    from fastapi.responses import Response

    from .templating import _asset_version

    with open("static/sw.js", encoding="utf-8") as fh:
        body = fh.read().replace("__ASSET_V__", _asset_version())
    return Response(
        body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(projects.router)
app.include_router(warehouse.router)
app.include_router(suppliers.router)
app.include_router(locations.router)
app.include_router(categories.router)
app.include_router(scan.router)
app.include_router(hub.router)
app.include_router(expenses_router.router)
app.include_router(tasks.router)
app.include_router(stats.router)
app.include_router(settings_router.router)
app.include_router(webhooks.router)


@app.exception_handler(_RedirectToLogin)
async def redirect_to_login(request: Request, exc: _RedirectToLogin):
    return RedirectResponse(url="/login", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
