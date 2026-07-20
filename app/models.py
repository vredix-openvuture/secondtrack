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
    in_production = "in_production"  # actively being worked on
    archived = "archived"           # finished, stored for later sale
    sold = "sold"                   # sold / done


class ProjectKind(str, enum.Enum):
    customer = "customer"  # built for a specific customer → invoice them
    shop = "shop"          # in-house production for the shop → sold via shop


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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.in_production, index=True
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


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # NULL project_id == the part currently lives in the virtual warehouse.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
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

    @property
    def in_warehouse(self) -> bool:
        return self.project_id is None


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
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    expense_date: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
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
