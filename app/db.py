from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base, Setting, User

settings = get_settings()

# Ensure the directory for the SQLite file exists.
_db_dir = os.path.dirname(os.path.abspath(settings.db_path))
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- Settings helpers (DB-backed, UI-editable) ----

DEFAULT_SETTINGS = {
    "hourly_rate": None,   # filled from env on first run
    "currency": None,      # filled from env on first run
}


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def _ensure_columns() -> None:
    """Tiny migration: add columns introduced after the first release.
    SQLite supports ADD COLUMN; create_all only creates missing *tables*."""
    from sqlalchemy import text

    wanted = {
        "projects": [
            ("image_path", "VARCHAR(255)"),
            ("vikunja_task_id", "VARCHAR(64)"),
            ("kind", "VARCHAR(20) DEFAULT 'customer'"),
            ("number", "VARCHAR(32)"),
            ("title", "VARCHAR(200)"),
            ("customer_id", "INTEGER"),
        ],
        "parts": [
            ("image_path", "VARCHAR(255)"),
            ("device_id", "INTEGER"),
            ("source_expense_id", "INTEGER"),
            ("set_id", "INTEGER"),
        ],
        "users": [("display_name", "VARCHAR(120)")],
        "work_sessions": [("hourly_rate", "FLOAT")],
        "order_invoices": [
            ("reminder_sent_at", "DATETIME"),
            ("dunning_sent_at", "DATETIME"),
        ],
        "expenses": [
            ("image_path", "VARCHAR(255)"),
            ("name", "VARCHAR(200)"),
            ("bucket", "VARCHAR(20)"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, ddl in cols:
                if name not in existing:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    )


def new_project_number(db: Session) -> str:
    """PJ-YYYYMMDD-XXXX with a collision-checked 4-char [A-Z0-9] suffix.
    Server-side generation (never Math.random in the client)."""
    import secrets
    import string
    from datetime import datetime as _dt

    from .models import Project

    day = _dt.utcnow().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        number = f"PJ-{day}-{''.join(secrets.choice(alphabet) for _ in range(4))}"
        if not db.query(Project).filter(Project.number == number).first():
            return number
    return f"PJ-{day}-{secrets.token_hex(3).upper()}"  # extremely unlikely


def _backfill_projects() -> None:
    """P2 of the projects rework: give every legacy project a number + title and
    a Device cloned from its device-era fields, then move its parts onto that
    device. Idempotent — projects that already have a number are skipped, so it
    is safe to run on every startup."""
    from .models import Device, DeviceStatus, Part, Project, ProjectStatus

    dev_status = {
        ProjectStatus.in_production: DeviceStatus.in_production,
        ProjectStatus.archived: DeviceStatus.archived,
        ProjectStatus.sold: DeviceStatus.sold,
    }
    with SessionLocal() as db:
        for p in db.query(Project).all():
            if p.number:
                continue  # already migrated
            p.number = new_project_number(db)
            if not p.title:
                p.title = p.name
            if not db.query(Device).filter(Device.project_id == p.id).first():
                dev = Device(
                    project_id=p.id,
                    name=p.name or p.title or "Gerät",
                    status=dev_status.get(p.status, DeviceStatus.in_production),
                    purchase_price=p.purchase_price or 0.0,
                    sale_price=p.sale_price,
                    woo_product_id=p.woo_product_id,
                    image_path=p.image_path,
                )
                db.add(dev)
                db.flush()  # obtain dev.id
                for part in db.query(Part).filter(Part.project_id == p.id).all():
                    if part.device_id is None:
                        part.device_id = dev.id
            db.commit()


def _remap_project_status() -> None:
    """P3 status remap: legacy device-era project statuses → new container
    lifecycle. Idempotent — only rows still holding a legacy value change."""
    from sqlalchemy import text

    mapping = {
        "in_production": "in_progress",
        "archived": "done",
        "sold": "invoiced",
    }
    with engine.begin() as conn:
        for old, new in mapping.items():
            conn.execute(
                text("UPDATE projects SET status = :new WHERE status = :old"),
                {"new": new, "old": old},
            )


def init_db() -> None:
    """Create tables, seed the admin user and default settings."""
    from passlib.hash import bcrypt

    Base.metadata.create_all(engine)
    _ensure_columns()

    with SessionLocal() as db:
        # Seed admin user if there are none.
        if db.query(User).count() == 0:
            db.add(
                User(
                    username=settings.admin_user,
                    password_hash=bcrypt.hash(settings.admin_password),
                )
            )
            db.commit()

        # Seed default settings from env if missing.
        if get_setting(db, "hourly_rate") is None:
            set_setting(db, "hourly_rate", str(settings.default_hourly_rate))
        if get_setting(db, "currency") is None:
            set_setting(db, "currency", settings.currency)

    # P2 backfill (idempotent): legacy projects → number + device + parts move.
    _backfill_projects()
    # P3 status remap (idempotent): legacy statuses → new lifecycle values.
    _remap_project_status()
