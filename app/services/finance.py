from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db import get_setting
from ..models import Part, PartOrigin, Project, ProjectStatus, WorkSession


def global_hourly_rate(db: Session) -> float:
    raw = get_setting(db, "hourly_rate", "0") or "0"
    try:
        return float(raw)
    except ValueError:
        return 0.0


def project_hourly_rate(db: Session, project: Project) -> float:
    if project.hourly_rate is not None:
        return project.hourly_rate
    return global_hourly_rate(db)


@dataclass
class ProjectFinance:
    project: Project
    rate: float
    parts: list[Part]
    sessions: list[WorkSession]

    # Money figures
    device_cost: float        # acquisition price of the whole device
    parts_purchase_cost: float  # cost of purchased parts installed
    material_cost: float      # device_cost + parts_purchase_cost
    parts_value: float        # sum of installed parts' sale price
    hours: float
    labor_value: float        # hours * rate
    build_total: float        # parts_value + labor_value (suggested quote)
    sale_price: float         # listing price (explicit, else build_total)
    gross_profit: float       # sale_price - material_cost
    net_profit: float         # sale_price - material_cost - labor_value


def compute_project(db: Session, project: Project) -> ProjectFinance:
    rate = project_hourly_rate(db, project)
    parts = [p for p in project.parts]
    sessions = list(project.sessions)

    device_cost = project.purchase_price or 0.0
    parts_purchase_cost = sum(
        (p.purchase_price or 0.0)
        for p in parts
        if p.origin == PartOrigin.purchased
    )
    material_cost = device_cost + parts_purchase_cost
    parts_value = sum((p.sale_price or 0.0) for p in parts)
    hours = sum((s.hours or 0.0) for s in sessions)
    # Labor value respects a per-session rate override, falling back to the
    # project/global rate.
    labor_value = sum(
        (s.hours or 0.0) * (s.hourly_rate if s.hourly_rate is not None else rate)
        for s in sessions
    )
    # Suggested price must at least recover the device purchase price, plus the
    # resale value of installed parts, plus labor. (Purchased-part cost is not
    # added separately — it's already reflected in each part's sale price.)
    build_total = device_cost + parts_value + labor_value
    sale_price = project.sale_price if project.sale_price is not None else build_total
    gross_profit = sale_price - material_cost
    net_profit = sale_price - material_cost - labor_value

    return ProjectFinance(
        project=project,
        rate=rate,
        parts=parts,
        sessions=sessions,
        device_cost=device_cost,
        parts_purchase_cost=parts_purchase_cost,
        material_cost=material_cost,
        parts_value=parts_value,
        hours=hours,
        labor_value=labor_value,
        build_total=build_total,
        sale_price=sale_price,
        gross_profit=gross_profit,
        net_profit=net_profit,
    )


@dataclass
class Stats:
    total_hours: float
    total_labor_value: float
    material_expenses: float       # all device + purchased-part costs (projects + warehouse)
    warehouse_stock_cost: float    # purchased parts sitting in the warehouse
    projected_sale_value: float    # listing value of unsold projects
    projected_gross_profit: float  # sale value - material costs of those projects
    projected_net_profit: float    # minus labor value
    active_count: int
    archived_count: int
    sold_count: int
    per_project: list[ProjectFinance]


def compute_stats(db: Session) -> Stats:
    projects = db.query(Project).all()
    per_project = [compute_project(db, p) for p in projects]

    total_hours = sum(f.hours for f in per_project)
    total_labor_value = sum(f.labor_value for f in per_project)

    # Warehouse = parts with no project.
    warehouse_parts = db.query(Part).filter(Part.project_id.is_(None)).all()
    warehouse_stock_cost = sum(
        (p.purchase_price or 0.0)
        for p in warehouse_parts
        if p.origin == PartOrigin.purchased
    )

    material_expenses = (
        sum(f.material_cost for f in per_project) + warehouse_stock_cost
    )

    unsold = [f for f in per_project if f.project.status != ProjectStatus.sold]
    projected_sale_value = sum(f.sale_price for f in unsold)
    projected_gross_profit = sum(f.gross_profit for f in unsold)
    projected_net_profit = sum(f.net_profit for f in unsold)

    return Stats(
        total_hours=total_hours,
        total_labor_value=total_labor_value,
        material_expenses=material_expenses,
        warehouse_stock_cost=warehouse_stock_cost,
        projected_sale_value=projected_sale_value,
        projected_gross_profit=projected_gross_profit,
        projected_net_profit=projected_net_profit,
        active_count=sum(
            1 for p in projects if p.status == ProjectStatus.in_production
        ),
        archived_count=sum(
            1 for p in projects if p.status == ProjectStatus.archived
        ),
        sold_count=sum(1 for p in projects if p.status == ProjectStatus.sold),
        per_project=per_project,
    )
