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
        ],
        "parts": [("image_path", "VARCHAR(255)")],
        "users": [("display_name", "VARCHAR(120)")],
        "work_sessions": [("hourly_rate", "FLOAT")],
        "order_invoices": [
            ("reminder_sent_at", "DATETIME"),
            ("dunning_sent_at", "DATETIME"),
        ],
        "expenses": [("image_path", "VARCHAR(255)")],
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
