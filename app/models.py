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
    customer = "customer"  # built for a specific customer → invoice them
    shop = "shop"          # in-house production for the shop → sold via shop


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
    devices: Mapped[list["Device"]] = relationship(
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

    origin: Mapped[PartOrigin] = mapped_column(
        Enum(PartOrigin), default=PartOrigin.purchased
    )
    # What it cost to buy (NULL/0 for harvested parts).
    purchase_price: Mapped[float | None] = mapped_column(Float, nullable=True)
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

    @property
    def in_warehouse(self) -> bool:
        return self.project_id is None and self.device_id is None


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

    invoiceninja_id: Mapped[str] = mapped_column(String(64), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dunning_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
