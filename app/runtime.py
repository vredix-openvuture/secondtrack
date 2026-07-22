"""DB-backed runtime configuration for connections, email and behaviour.

Values live in the settings table (prefixed 'cfg_') so they can be edited in
the UI. Initial defaults come from environment variables (.env). A small
in-process cache is loaded at startup and refreshed on save (the app runs as a
single uvicorn worker)."""
from __future__ import annotations

from .config import get_settings

_env = get_settings()

# key -> default value (string). Booleans stored as "1"/"0".
DEFAULTS: dict[str, str] = {
    # WooCommerce
    "woo_enabled": "1" if _env.woo_enabled else "0",
    "woo_url": _env.woo_url,
    "woo_key": _env.woo_key,
    "woo_secret": _env.woo_secret,
    "woo_order_statuses": _env.woo_order_statuses,
    "woo_webhook_enabled": "0",
    "woo_webhook_secret": "",
    # Polling fallback (no WordPress changes needed)
    "woo_poll_enabled": "0",
    "woo_poll_interval": "5",   # minutes
    "woo_poll_since": "",       # only orders created at/after this are auto-receipted
    # InvoiceNinja
    "in_enabled": "1" if _env.invoiceninja_enabled else "0",
    "in_url": _env.invoiceninja_url,
    "in_token": _env.invoiceninja_token,
    "in_auto_send": "1" if _env.invoiceninja_auto_send else "0",
    # Vikunja
    "vikunja_enabled": "1" if _env.vikunja_enabled else "0",
    "vikunja_url": _env.vikunja_url,
    "vikunja_token": _env.vikunja_token,
    "vikunja_parent": _env.vikunja_parent_project,
    # Nextcloud (WebDAV document storage)
    "nc_enabled": "1" if _env.nextcloud_enabled else "0",
    "nc_url": _env.nextcloud_url,
    "nc_user": _env.nextcloud_user,
    "nc_pass": _env.nextcloud_pass,
    "nc_base_path": _env.nextcloud_base_path,
    "nc_auto_archive": "1" if _env.nextcloud_auto_archive else "0",
    # eBay (price suggestions)
    "ebay_enabled": "1" if _env.ebay_enabled else "0",
    "ebay_client_id": _env.ebay_client_id,
    "ebay_client_secret": _env.ebay_client_secret,
    "ebay_marketplace": _env.ebay_marketplace,
    # Email — provider: "secondtrack" (own SMTP) or "invoiceninja" (IN sends)
    "email_provider": "secondtrack",
    "email_enabled": "0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_security": "tls",  # tls | ssl | none
    "mail_from_name": "secondtrack",
    "mail_from_email": "",
    "email_auto": "0",       # run daily reminder/dunning automatically
    "reminder_days": "0",    # days after due date to send a payment reminder
    "dunning_days": "30",    # days after due date to send a dunning notice
    # Email templates (placeholders: {client} {number} {amount} {due_date} {link} {company})
    "tpl_invoice_subject": "Invoice {number}",
    "tpl_invoice_body": (
        "Hello {client},\n\nplease find attached invoice {number} for {amount}, "
        "due on {due_date}.\n\nThank you for your business!\n{company}"
    ),
    "tpl_reminder_subject": "Payment reminder for invoice {number}",
    "tpl_reminder_body": (
        "Hello {client},\n\nthis is a friendly reminder that invoice {number} "
        "for {amount} was due on {due_date} and appears to be unpaid.\n\n"
        "Please disregard this message if payment has already been made.\n\n{company}"
    ),
    "tpl_dunning_subject": "Overdue notice for invoice {number}",
    "tpl_dunning_body": (
        "Hello {client},\n\ninvoice {number} for {amount} was due on {due_date} "
        "and is now significantly overdue. Please arrange payment as soon as "
        "possible to avoid further action.\n\n{company}"
    ),
    "tpl_receipt_subject": "Your receipt {number}",
    "tpl_receipt_body": (
        "Hello {client},\n\nthank you for your purchase! Please find attached "
        "your receipt {number} for {amount} (paid).\n\n{company}"
    ),
}

_cache: dict[str, str] = {}


def load(db) -> None:
    from .db import get_setting

    for key, default in DEFAULTS.items():
        val = get_setting(db, "cfg_" + key, None)
        _cache[key] = default if val is None else val


def get(key: str, default: str = "") -> str:
    if not _cache:  # not yet loaded (e.g. unit context)
        return DEFAULTS.get(key, default)
    return _cache.get(key, DEFAULTS.get(key, default))


def get_bool(key: str) -> bool:
    return str(get(key, "0")).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(str(get(key)).strip())
    except (ValueError, TypeError):
        return default


def save(db, mapping: dict[str, str]) -> None:
    from .db import set_setting

    for key, value in mapping.items():
        if key in DEFAULTS:
            set_setting(db, "cfg_" + key, value)
            _cache[key] = value
