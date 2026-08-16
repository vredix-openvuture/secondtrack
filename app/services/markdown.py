from __future__ import annotations

import os
import re
from datetime import date

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Project
from .finance import compute_project

settings = get_settings()

_STATUS_LABEL = {
    "in_production": "In Produktion",
    "archived": "Eingelagert",
    "sold": "Verkauft",
}


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {settings.currency}"


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", s) or "projekt"


def render_project_markdown(db: Session, project: Project) -> str:
    from ..db import get_setting

    f = compute_project(db, project)
    # Relative /uploads paths mean nothing once the file sits in a vault, so the
    # export needs absolute ones. Without a public base URL configured the
    # gallery is skipped rather than exported broken.
    base = (get_setting(db, "public_base_url", "") or "").rstrip("/")
    lines: list[str] = []

    # YAML frontmatter for Obsidian.
    lines += [
        "---",
        f'title: "{project.name}"',
        "type: secondtrack-projekt",
        f"status: {project.status.value}",
        f"erstellt: {project.created_at.date().isoformat() if project.created_at else date.today().isoformat()}",
        f"materialkosten: {f.material_cost:.2f}",
        f"verkaufspreis: {f.sale_price:.2f}",
        f"arbeitsstunden: {f.hours:.2f}",
        f"stundensatz: {f.rate:.2f}",
        f"gewinn_brutto: {f.gross_profit:.2f}",
        "tags: [secondtrack]",
        "---",
        "",
        f"# {project.name}",
        "",
        f"**Status:** {_STATUS_LABEL.get(project.status.value, project.status.value)}  ",
        f"**Materialkosten:** {_fmt(f.material_cost)}",
        "",
    ]

    if project.description:
        lines += [project.description, ""]

    # Item table — a set is one row, same as on the project page. Listing its
    # members as well would show (and sum) the same purchase twice.
    lines += ["## Verbaute Teile", ""]
    if f.items:
        lines += [
            "| Objekt | Menge | Herkunft | Einkauf | Verkaufswert |",
            "| --- | ---: | --- | ---: | ---: |",
        ]
        for it in f.items:
            if it["kind"] == "set":
                origin = "Set"
            else:
                origin = "Gekauft" if it["bought"] else "Ausgebaut"
            # Both money columns are line totals, so the quantity behind them has
            # to be on the row as well; without it nine fans read as one.
            qty = it.get("qty") or 1
            lines.append(
                f"| {it['obj'].name} | {qty} | {origin} "
                f"| {_fmt(it['purchase'])} | {_fmt(it['sale'])} |"
            )
        lines += [
            f"| **Summe** | | | **{_fmt(f.parts_purchase_cost)}** "
            f"| **{_fmt(f.parts_value)}** |",
        ]
    else:
        lines.append("_Keine Teile erfasst._")
    lines.append("")

    # Reference photos
    if project.images and base:
        lines += ["## Referenzbilder", ""]
        for img in sorted(project.images, key=lambda i: i.id):
            lines.append(f"![{img.caption or ''}]({base}{img.path})")
        lines.append("")

    # Work sessions
    lines += ["## Arbeitszeiten", ""]
    if f.sessions:
        lines += [
            "| Datum | Stunden | Beschreibung |",
            "| --- | ---: | --- |",
        ]
        for s in sorted(f.sessions, key=lambda x: x.work_date):
            desc = (s.description or "").replace("\n", " ")
            hrs = f"{s.hours:.2f}".replace(".", ",")
            lines.append(f"| {s.work_date.isoformat()} | {hrs} | {desc} |")
        hrs_total = f"{f.hours:.2f}".replace(".", ",")
        lines.append(f"| **Summe** | **{hrs_total}** | |")
    else:
        lines.append("_Keine Arbeitszeiten erfasst._")
    lines.append("")

    # Summary
    lines += [
        "## Zusammenfassung",
        "",
        f"- **Teile (Verkaufswert):** {_fmt(f.parts_value)}",
        f"- **Arbeitszeit:** {f.hours:.2f} h × {_fmt(f.rate)} = {_fmt(f.labor_value)}".replace(".", ",", 1),
        f"- **Vorgeschlagener Gesamtpreis:** {_fmt(f.build_total)}",
        f"- **Listenpreis (VK):** {_fmt(f.sale_price)}",
        "",
        f"- Materialkosten: {_fmt(f.material_cost)}",
        f"- Bruttogewinn (VK − Material): **{_fmt(f.gross_profit)}**",
        f"- Gewinn nach Arbeitszeit: {_fmt(f.net_profit)}",
        "",
    ]

    return "\n".join(lines)


def export_project_to_file(db: Session, project: Project) -> str:
    """Write the project markdown to the configured export dir. Returns the path."""
    content = render_project_markdown(db, project)
    os.makedirs(settings.export_dir, exist_ok=True)
    filename = f"{project.id:04d}-{_slug(project.name)}.md"
    path = os.path.join(settings.export_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
