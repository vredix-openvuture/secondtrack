from __future__ import annotations

from fastapi.templating import Jinja2Templates

from .config import get_settings

settings = get_settings()

templates = Jinja2Templates(directory="templates")


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hours_fmt(value: float | None) -> str:
    if value is None:
        return "0"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".replace(".", ",")


def md(value):
    from .services.mdrender import render

    return render(value)


templates.env.filters["money"] = money
templates.env.filters["hours"] = hours_fmt
templates.env.filters["md"] = md
templates.env.globals["currency"] = settings.currency


def ctx(request, db, active: str = "", **extra):
    """Build a base template context with common values."""
    from .db import get_setting
    from .i18n import DEFAULT_LANG, make_translator

    lang = get_setting(db, "language", DEFAULT_LANG) or DEFAULT_LANG
    data = {
        "request": request,
        "active": active,
        "lang": lang,
        "_": make_translator(lang),
        "accent": get_setting(db, "accent", "#fb6734") or "#fb6734",
        "currency": get_setting(db, "currency", settings.currency),
        "wallpaper": {
            "url": get_setting(db, "wallpaper_path", "") or "",
            "blur": get_setting(db, "wallpaper_blur", "0") or "0",
            "dark": get_setting(db, "wallpaper_dark", "40") or "40",
        },
        "style": {
            "accent": get_setting(db, "accent", "#fb6734") or "#fb6734",
            "accent2": get_setting(db, "accent2", "#ce3737") or "#ce3737",
            "bg": get_setting(db, "style_bg", "#26121b") or "#26121b",
            "radius": get_setting(db, "style_radius", "10") or "10",
            "font": get_setting(db, "style_font", "system") or "system",
            "density": get_setting(db, "style_density", "comfortable") or "comfortable",
            "glass": get_setting(db, "style_glass", "0") or "0",
            "card_opacity": get_setting(db, "style_card_opacity", "100") or "100",
            "sidebar": get_setting(db, "style_sidebar", "closed") or "closed",
        },
    }
    data.update(extra)
    return data
