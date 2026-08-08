from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db import get_setting
from ..models import Part, PartOrigin, PartSet, Project, ProjectStatus, WorkSession


def project_items(db: Session, project: Project) -> list[dict]:
    """Every warehouse object assigned to this project, flat. A set counts as
    one item at its own price and swallows its members, so a set and its parts
    are never both billed. Single source of truth for the item list, the
    calculation and the invoice — they must never disagree."""
    sets = (
        db.query(PartSet).filter(PartSet.project_id == project.id)
        .order_by(PartSet.name).all()
    )
    grouped = {p.id for ps in sets for p in ps.parts}
    items: list[dict] = [
        {"kind": "set", "obj": ps, "purchase": ps.purchase_price or 0.0,
         "sale": ps.sale_price or 0.0, "bought": True}
        for ps in sets
    ]
    items += [
        {"kind": "part", "obj": p, "purchase": p.purchase_price or 0.0,
         "sale": p.sale_price or 0.0, "bought": p.origin == PartOrigin.purchased}
        for p in db.query(Part).filter(Part.project_id == project.id)
        .order_by(Part.name).all()
        if p.id not in grouped
    ]
    return items


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
    items: list[dict]         # what the project page shows and the invoice bills
    sessions: list[WorkSession]

    # Money figures
    parts_purchase_cost: float  # cost of purchased items assigned to the project
    material_cost: float      # parts_purchase_cost (+ legacy project price)
    parts_value: float        # sum of assigned items' sale price
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
    items = project_items(db, project)

    # Cost and value both come from the assigned items — the same rows the page
    # lists, so the table and the calculation can never drift apart.
    # `project.purchase_price` is the pre-warehouse field, honoured only for old
    # projects that never got an item, else it double-counts against one.
    parts_purchase_cost = sum(i["purchase"] for i in items if i["bought"])
    legacy_cost = 0.0 if items else (project.purchase_price or 0.0)
    material_cost = parts_purchase_cost + legacy_cost
    parts_value = sum(i["sale"] for i in items)
    hours = sum((s.hours or 0.0) for s in sessions)
    # Labor value respects a per-session rate override, falling back to the
    # project/global rate.
    labor_value = sum(
        (s.hours or 0.0) * (s.hourly_rate if s.hourly_rate is not None else rate)
        for s in sessions
    )
    # Suggested price = resale value of the assigned items plus labor. Their
    # purchase cost is not added separately — it is already reflected in each
    # item's sale price.
    build_total = parts_value + labor_value + legacy_cost
    # Listing price: explicit if set on the project, else the suggested total.
    if project.sale_price is not None:
        sale_price = project.sale_price
    else:
        sale_price = build_total
    gross_profit = sale_price - material_cost
    net_profit = sale_price - material_cost - labor_value

    return ProjectFinance(
        project=project,
        rate=rate,
        parts=parts,
        items=items,
        sessions=sessions,
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

    unsold = [f for f in per_project if f.project.status != ProjectStatus.invoiced]
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
            1 for p in projects
            if p.status in (ProjectStatus.open, ProjectStatus.in_progress)
        ),
        archived_count=sum(
            1 for p in projects if p.status == ProjectStatus.done
        ),
        sold_count=sum(1 for p in projects if p.status == ProjectStatus.invoiced),
        per_project=per_project,
    )
