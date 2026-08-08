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
            ("type_id", "INTEGER"),
        ],
        "parts": [
            ("image_path", "VARCHAR(255)"),
            ("device_id", "INTEGER"),
            ("source_expense_id", "INTEGER"),
            ("set_id", "INTEGER"),
            ("quantity", "INTEGER DEFAULT 1"),
            # Warehouse rework (W1/W2/W4/W6)
            ("category_id", "INTEGER"),
            ("supplier_id", "INTEGER"),
            ("location_id", "INTEGER"),
            ("code", "VARCHAR(32)"),
            ("attributes", "TEXT"),
            ("condition", "VARCHAR(20)"),
            ("serial_no", "VARCHAR(120)"),
            ("mpn", "VARCHAR(120)"),
            ("ean", "VARCHAR(64)"),
            ("warranty_until", "DATE"),
            ("purchase_date", "DATE"),
            ("min_stock", "INTEGER"),
            ("unit", "VARCHAR(20)"),
            ("extra", "TEXT"),
        ],
        "categories": [
            ("color", "VARCHAR(16)"),
        ],
        "sets": [
            # Warehouse rework (W5)
            ("kind", "VARCHAR(20) DEFAULT 'purchase_lot'"),
            ("status", "VARCHAR(20)"),
            ("sellable", "BOOLEAN DEFAULT 0"),
            ("condition", "VARCHAR(20)"),
            ("notes", "TEXT"),
            ("code", "VARCHAR(32)"),
            ("location_id", "INTEGER"),
            ("source_project_id", "INTEGER"),
            ("project_id", "INTEGER"),
        ],
        "users": [("display_name", "VARCHAR(120)")],
        "work_sessions": [("hourly_rate", "FLOAT")],
        "order_invoices": [
            ("reminder_sent_at", "DATETIME"),
            ("dunning_sent_at", "DATETIME"),
            ("customer_id", "INTEGER"),
            ("vikunja_task_id", "VARCHAR(64)"),
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


def _backfill_codes() -> None:
    """Give existing warehouse parts and sets a scan code (idempotent). New
    rows get their code on creation; this covers rows from before the rework."""
    from .models import Category, Part, PartSet
    from .routers.categories import CATEGORY_COLORS
    from .services import codes

    with SessionLocal() as db:
        for part in db.query(Part).filter(Part.code.is_(None)).all():
            part.code = codes.generate(db, "part")
        for ps in db.query(PartSet).filter(PartSet.code.is_(None)).all():
            ps.code = codes.generate(db, "set")
        # Give pre-existing categories a colour so cards/tags are tinted.
        for i, cat in enumerate(
            db.query(Category).filter(Category.color.is_(None)).order_by(Category.position).all()
        ):
            cat.color = CATEGORY_COLORS[i % len(CATEGORY_COLORS)]
        # Migrate legacy per-column optional data into the `extra` JSON blob so
        # the new global optional-fields system owns it. Runs once (extra IS NULL).
        import json as _json
        for part in db.query(Part).filter(Part.extra.is_(None)).all():
            d: dict = {}
            if part.serial_no:
                d["serial_no"] = part.serial_no
            if part.mpn:
                d["mpn"] = part.mpn
            if part.ean:
                d["ean"] = part.ean
            if part.unit:
                d["unit"] = part.unit
            if part.min_stock is not None:
                d["min_stock"] = part.min_stock
            if part.purchase_date:
                d["purchase_date"] = part.purchase_date.isoformat()
            if part.warranty_until:
                d["warranty_until"] = part.warranty_until.isoformat()
            part.extra = _json.dumps(d, ensure_ascii=False)
        db.commit()


def _migrate_devices_to_parts() -> None:
    """Devices predate the warehouse: a project-only container with no code,
    category, supplier, location or purchase expense. A device is just a
    warehouse item, so each one becomes a Part assigned to its project and its
    child parts become siblings on that same project — one flat item list.

    The device row is removed once its data lives on the part; re-running finds
    nothing left to do. The pre-update database backup is the way back.
    """
    from .models import Device, Part, PartOrigin
    from .services import codes

    def _is_placeholder(dev, has_parts: bool) -> bool:
        """The old create_project gave every project a device named after it,
        priced at zero. Those carry nothing — migrating them would put a 0.00
        item named like the project into every single project."""
        return (
            not has_parts
            and not dev.purchase_price
            and not dev.sale_price
            and not dev.image_path
            and not dev.woo_product_id
            and dev.project is not None
            and dev.name in (dev.project.name, dev.project.title)
        )

    with SessionLocal() as db:
        devices = db.query(Device).all()
        if not devices:
            return
        for dev in devices:
            kids = db.query(Part).filter(Part.device_id == dev.id).all()
            if _is_placeholder(dev, bool(kids)):
                continue  # nothing to carry over
            db.add(
                Part(
                    name=dev.name,
                    project_id=dev.project_id,
                    origin=(
                        PartOrigin.purchased if dev.purchase_price
                        else PartOrigin.harvested
                    ),
                    purchase_price=dev.purchase_price or None,
                    sale_price=dev.sale_price,
                    image_path=dev.image_path,
                    quantity=1,
                    code=codes.generate(db, "part"),
                )
            )
            # A device could carry the shop listing; the project keeps it.
            if dev.woo_product_id and dev.project and not dev.project.woo_product_id:
                dev.project.woo_product_id = dev.woo_product_id
            # Children become siblings — the hierarchy is what we are dropping.
            for p in kids:
                p.device_id = None
                if p.project_id is None:
                    p.project_id = dev.project_id
        db.flush()  # detach every part before the FK targets disappear
        for dev in devices:
            db.delete(dev)
        db.commit()


def _drop_placeholder_project_items() -> None:
    """Undo the first cut of _migrate_devices_to_parts, which still converted the
    auto-created placeholder devices. It left every project holding an item named
    after itself at 0.00, carrying nothing else.

    Only rows that are empty in every respect are removed, so there is nothing to
    lose by construction: no price, image, receipt, note, category, supplier,
    location or attribute, a single unit, and a name that duplicates the project.
    """
    from .models import Part, Project

    with SessionLocal() as db:
        if get_setting(db, "placeholder_items_dropped") == "1":
            return
        for p in db.query(Part).filter(Part.project_id.isnot(None)).all():
            project: Project | None = db.get(Project, p.project_id)
            if (
                project is not None
                and p.name in (project.name, project.title)
                and not p.purchase_price
                and not p.sale_price
                and not p.image_path
                and p.source_expense_id is None
                and p.set_id is None
                and p.category_id is None
                and p.supplier_id is None
                and p.location_id is None
                and not p.notes
                and not p.serial_no
                and not p.mpn
                and not p.ean
                and not p.attributes
                # _backfill_codes fills `extra` with an empty object on a later
                # startup, so both spellings of "nothing" have to count.
                and (p.extra or "").strip() in ("", "{}")
                and (p.quantity or 1) <= 1
            ):
                db.delete(p)
        set_setting(db, "placeholder_items_dropped", "1")
        db.commit()


def _seed_project_types() -> None:
    """The two hard-coded kinds become editable rows, so the user can add their
    own (Repair, Conversion, …). Idempotent: seeds only what is missing and
    assigns a type to projects that still have none."""
    from .models import Project, ProjectKind, ProjectType

    with SessionLocal() as db:
        wanted = [("Customer order", False, 0), ("Shop production", True, 1)]
        for name, shop_stock, pos in wanted:
            if not db.query(ProjectType).filter(ProjectType.name == name).first():
                db.add(ProjectType(name=name, shop_stock=shop_stock, position=pos))
        db.commit()

        by_shop = {
            t.shop_stock: t for t in db.query(ProjectType).order_by(ProjectType.position)
        }
        for p in db.query(Project).filter(Project.type_id.is_(None)).all():
            p.type_id = by_shop[p.kind == ProjectKind.shop].id
        db.commit()


def _default_font_fredoka() -> None:
    """One-time: Fredoka replaced the system stack as the app font, so installs
    still carrying the old 'system' default move over. The marker makes it run
    once, so switching back in the settings later sticks."""
    with SessionLocal() as db:
        if get_setting(db, "style_font_default_v2") == "1":
            return
        if get_setting(db, "style_font", "system") in (None, "", "system"):
            set_setting(db, "style_font", "fredoka")
        set_setting(db, "style_font_default_v2", "1")


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
    # Warehouse rework: give pre-existing parts/sets a scan code (idempotent).
    _backfill_codes()
    # One-time: move installs off the old 'system' font default onto Fredoka.
    _default_font_fredoka()
    # Devices become plain warehouse items on their project (idempotent).
    _migrate_devices_to_parts()
    # Clean up the empty placeholders the first cut of that migration created.
    _drop_placeholder_project_items()
    # The fixed customer/shop kinds become editable project types.
    _seed_project_types()
