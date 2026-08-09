from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ProjectStatus(str, enum.Enum):
    # New container-lifecycle statuses (used going forward).
    open = "open"                    # created, nothing done yet
    in_progress = "in_progress"      # being worked on
    done = "done"                    # finished, not yet invoiced
    invoiced = "invoiced"            # invoice created in InvoiceNinja
    # Legacy device-era statuses — kept until the P4 cleanup so finance/stats
    # and existing rows keep working during the projects rework.
    in_production = "in_production"
    archived = "archived"
    sold = "sold"


class ProjectKind(str, enum.Enum):
    """Legacy. Superseded by the user-editable ProjectType; kept so the
    migration can read the old column."""

    customer = "customer"  # built for a specific customer → invoice them
    shop = "shop"          # in-house production for the shop → sold via shop


class ProjectType(Base):
    """A project category the user can extend — "Repair", "Conversion", …

    `shop_stock` is the one thing a type has to declare: whether its builds may
    be stocked as sellable finished goods. Customer work never can, it gets
    invoiced; in-house production does. Without that flag a custom type would
    have no defined behaviour."""

    __tablename__ = "project_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    shop_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)


class CustomerKind(str, enum.Enum):
    invoiceninja = "invoiceninja"  # backed by an InvoiceNinja client
    internal = "internal"          # internal / no external invoicing


class DeviceStatus(str, enum.Enum):
    in_production = "in_production"  # being worked on
    archived = "archived"           # finished, stored for later sale
    sold = "sold"                   # sold


class PartOrigin(str, enum.Enum):
    purchased = "purchased"   # bought separately
    harvested = "harvested"   # removed from a project, no purchase cost


# --- Warehouse value sets (plain strings so ALTER TABLE ADD COLUMN stays trivial) ---

class PartCondition(str, enum.Enum):
    new = "new"
    used = "used"
    refurbished = "refurbished"
    defective = "defective"


class SetKind(str, enum.Enum):
    purchase_lot = "purchase_lot"  # bought together as one lot (the original set)
    assembly = "assembly"          # manufactured product / finished good


# Single-letter code prefixes used by the scan resolver (/s/<code>).
CODE_PREFIX = {"part": "P", "set": "S", "location": "L"}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    # Container model (projects rework): human project number + title + customer.
    number: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.open, index=True
    )
    kind: Mapped[ProjectKind] = mapped_column(
        Enum(ProjectKind), default=ProjectKind.customer, index=True
    )  # legacy, migrated into type_id
    type_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_types.id"), nullable=True, index=True
    )
    # What the whole device cost to acquire.
    purchase_price: Mapped[float] = mapped_column(Float, default=0.0)
    # Expected / target selling price (the listing price).
    sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Optional per-project hourly rate; falls back to the global setting.
    hourly_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Integration references (populated in phase 2/3).
    woo_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invoiceninja_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vikunja_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    parts: Mapped[list["Part"]] = relationship(
        back_populates="project", foreign_keys="Part.project_id"
    )
    sessions: Mapped[list["WorkSession"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    customer: Mapped["Customer | None"] = relationship(back_populates="projects")
    type: Mapped["ProjectType | None"] = relationship(foreign_keys=[type_id])
    devices: Mapped[list["Device"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    images: Mapped[list["ProjectImage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    reports: Mapped[list["Report"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Warehouse == both project_id and device_id are NULL. A part can sit
    # directly on a device (device_id) or loosely on a project (project_id only).
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id"), nullable=True, index=True
    )
    # The purchase this part came from (a set/lot buy → one expense, N parts).
    source_expense_id: Mapped[int | None] = mapped_column(
        ForeignKey("expenses.id"), nullable=True, index=True
    )
    # The set (grouping) this part belongs to, if any.
    set_id: Mapped[int | None] = mapped_column(
        ForeignKey("sets.id"), nullable=True, index=True
    )

    # Warehouse extensions (W1): category + supplier + physical location.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), nullable=True, index=True
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id"), nullable=True, index=True
    )
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    # Scan code (barcode/QR), unique across parts — resolved by /s/<code>.
    code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    # Category-specific fields, stored as a JSON object: {field_key: value}.
    attributes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Global optional fields (same schema for all products, editable in Settings),
    # stored as a JSON object: {field_key: value}.
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)

    origin: Mapped[PartOrigin] = mapped_column(
        Enum(PartOrigin), default=PartOrigin.purchased
    )
    # Intake data (W6). Plain strings/values, all optional.
    condition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    serial_no: Mapped[str | None] = mapped_column(String(120), nullable=True)
    mpn: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(64), nullable=True)
    warranty_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Reorder threshold: warn when quantity drops to/below this (NULL = no alert).
    min_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Unit of measure (pcs, m, kg, …).
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # What it cost to buy (NULL/0 for harvested parts).
    purchase_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How many identical units are in stock (e.g. 100 stickers). Prices are per unit.
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # Value it contributes when installed in a build.
    sale_price: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped["Project | None"] = relationship(
        back_populates="parts", foreign_keys=[project_id]
    )
    device: Mapped["Device | None"] = relationship(
        back_populates="parts", foreign_keys=[device_id]
    )
    part_set: Mapped["PartSet | None"] = relationship(
        back_populates="parts", foreign_keys=[set_id]
    )
    category: Mapped["Category | None"] = relationship(foreign_keys=[category_id])
    supplier: Mapped["Supplier | None"] = relationship(foreign_keys=[supplier_id])
    location: Mapped["StorageLocation | None"] = relationship(foreign_keys=[location_id])

    @property
    def in_warehouse(self) -> bool:
        return self.project_id is None and self.device_id is None

    @staticmethod
    def _parse_json_obj(raw) -> dict:
        import json

        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    @property
    def attrs(self) -> dict:
        """Parsed category-specific attributes ({} if unset/broken)."""
        return self._parse_json_obj(self.attributes)

    @property
    def extras(self) -> dict:
        """Parsed global optional-field values ({} if unset/broken)."""
        return self._parse_json_obj(self.extra)

    @property
    def low_stock(self) -> bool:
        # Reorder level lives in the global optional fields (key "min_stock");
        # fall back to the legacy column for parts not yet migrated.
        raw = self.extras.get("min_stock", None)
        try:
            ms = int(raw) if raw not in (None, "") else None
        except (ValueError, TypeError):
            ms = None
        if ms is None:
            ms = self.min_stock
        return ms is not None and (self.quantity or 0) <= ms


class PartSet(Base):
    """A purchase grouping (a bought set/lot): one price + one receipt, split
    across its member parts. The set's value is the sum of its parts; an own
    sale price is optional."""

    __tablename__ = "sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    purchase_price: Mapped[float] = mapped_column(Float, default=0.0)
    sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expense_id: Mapped[int | None] = mapped_column(
        ForeignKey("expenses.id"), nullable=True
    )
    # W5: a set is either a bought lot or a manufactured product (assembly).
    kind: Mapped[str] = mapped_column(String(20), default=SetKind.purchase_lot.value)
    # Assembly lifecycle: "wip" (being built) or "finished"/NULL (done). Only
    # meaningful for kind == assembly.
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Marked ready to sell / ship (a finished good on the shelf).
    sellable: Mapped[bool] = mapped_column(Boolean, default=False)
    condition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scan code (barcode/QR) — resolved by /s/<code>.
    code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    # If assembled out of a project build, remember where it came from.
    source_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    # Currently assigned to this project (NULL == on the shelf). Distinct from
    # source_project_id, which records origin and never changes.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parts: Mapped[list["Part"]] = relationship(
        back_populates="part_set", foreign_keys="Part.set_id"
    )
    location: Mapped["StorageLocation | None"] = relationship(foreign_keys=[location_id])

    @property
    def is_assembly(self) -> bool:
        return self.kind == SetKind.assembly.value

    @property
    def is_wip(self) -> bool:
        return self.kind == SetKind.assembly.value and self.status == "wip"

    @property
    def warehouse_parts(self) -> list["Part"]:
        """Members still sitting in the warehouse (not installed)."""
        return [p for p in self.parts if p.project_id is None and p.device_id is None]


class Category(Base):
    """A warehouse product category. Beyond the standard part fields it defines
    a set of category-specific fields (e.g. CPU → platform AM4/AM5) stored as a
    JSON schema in `fields_json`; a part's values live in `Part.attributes`."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    icon: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Accent colour (hex) used for the card glow + table tag of this category.
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Ordered list of field definitions, each:
    #   {"key","label","type"(text|number|select|bool|date),"options":[],"required":bool,"unit"}
    fields_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def fields(self) -> list[dict]:
        import json

        try:
            data = json.loads(self.fields_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []


class Supplier(Base):
    """A supplier/vendor a part or lot was bought from."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # My own customer/account number at this supplier.
    account_no: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StorageLocation(Base):
    """A physical storage place (room → rack → shelf → bin). Hierarchical via
    `parent_id`; every location has a scan code for barcode/QR labels."""

    __tablename__ = "storage_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parent: Mapped["StorageLocation | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["StorageLocation"]] = relationship(
        back_populates="parent",
        cascade="all",
        order_by="StorageLocation.name",
    )

    @property
    def path(self) -> str:
        """Full breadcrumb path 'Room › Rack › Bin' (guards against cycles)."""
        names, node, seen = [], self, set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            names.append(node.name)
            node = node.parent
        return " › ".join(reversed(names))


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[CustomerKind] = mapped_column(
        Enum(CustomerKind), default=CustomerKind.internal
    )
    # Set when this customer maps to an InvoiceNinja client.
    invoiceninja_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    projects: Mapped[list["Project"]] = relationship(back_populates="customer")


class Device(Base):
    """Legacy. A device was a project-only container from before the warehouse
    existed. `_migrate_devices_to_parts` in db.py turns each one into a plain
    warehouse part on its project and deletes the row; this class survives only
    so that migration can still read old databases. Nothing else uses it."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus), default=DeviceStatus.in_production, index=True
    )
    # What the device cost to acquire.
    purchase_price: Mapped[float] = mapped_column(Float, default=0.0)
    # Expected / target selling price (the listing price).
    sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    woo_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="devices")
    parts: Mapped[list["Part"]] = relationship(
        back_populates="device", foreign_keys="Part.device_id"
    )


class ProjectImage(Base):
    """A reference photo on a project: condition at handover, cable routing
    before disassembly, the type plate. Distinct from `Project.image_path`,
    which is the single picture identifying the project itself."""

    __tablename__ = "project_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    path: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="images")


class Report(Base):
    """A Markdown report/note attached to a project (uses the markdown editor)."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="reports")


class WorkSession(Base):
    __tablename__ = "work_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), index=True
    )
    work_date: Mapped[date] = mapped_column(Date, default=date.today)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hours: Mapped[float] = mapped_column(Float, default=0.0)
    # Optional per-session rate; falls back to the project/global rate.
    hourly_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="sessions")


class Expense(Base):
    """A business expense (purchase) with a receipt, mirrored to InvoiceNinja."""

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    expense_date: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Allocation: "project" (with project_id), "warehouse" or "advertisement".
    bucket: Mapped[str | None] = mapped_column(String(20), nullable=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    receipt_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoiceninja_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project | None"] = relationship()


class Setting(Base):
    """Simple key/value store for UI-editable settings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class InvoiceSource(str, enum.Enum):
    woo = "woo"          # generated from a WooCommerce order
    project = "project"  # generated from a secondtrack project


class OrderInvoice(Base):
    """Links a WooCommerce order or a project to the InvoiceNinja invoice
    we created for it. Keeps the hub coherent and prevents double invoicing."""

    __tablename__ = "order_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[InvoiceSource] = mapped_column(Enum(InvoiceSource), index=True)

    woo_order_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, unique=True, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    # Local customer this order belongs to (created/linked from its billing data).
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )
    # The Vikunja fulfillment task ("what to pack & ship") created for this order.
    vikunja_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    invoiceninja_id: Mapped[str] = mapped_column(String(64), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dunning_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
