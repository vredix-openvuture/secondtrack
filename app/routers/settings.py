from __future__ import annotations

import base64
import io
from datetime import datetime

import pyotp
import qrcode
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import runtime
from ..auth import hash_password, require_login, verify_password
from ..config import get_settings as get_app_settings
from ..db import get_db, get_setting, set_setting
from ..i18n import DEFAULT_LANG, LANGUAGES
from ..models import User
from ..services import emails, mailer
from ..services.uploads import delete_image, save_image
from ..templating import ctx, templates

router = APIRouter(prefix="/settings")
app_settings = get_app_settings()


def _bx(v: str) -> str:
    return "1" if str(v).strip().lower() in ("1", "true", "yes", "on") else "0"


def _secret(new: str, key: str) -> str:
    """Keep the stored secret when the field is submitted blank. We never render
    the real value into the page (only a masked placeholder), so an empty submit
    means 'leave unchanged' rather than 'clear it'."""
    new = (new or "").strip()
    return new if new else (runtime.get(key) or "")


def _qr_data_uri(uri: str) -> str:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"


@router.get("")
async def settings_page(
    request: Request,
    tab: str = "general",
    sub: str = "woo",
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    enrolling_uri = request.session.pop("enroll_qr", None)
    rt = {k: runtime.get(k) for k in runtime.DEFAULTS}
    return templates.TemplateResponse(
        "settings.html",
        ctx(
            request, db, active="settings",
            tab=tab, sub=sub, user=user,
            display_name=user.display_name or "",
            hourly_rate=get_setting(db, "hourly_rate", "0"),
            currency=get_setting(db, "currency", app_settings.currency),
            languages=LANGUAGES,
            current_lang=get_setting(db, "language", DEFAULT_LANG) or DEFAULT_LANG,
            rt=rt,
            rt_bool={k: runtime.get_bool(k) for k in runtime.DEFAULTS},
            email_on=emails.sending_enabled(),
            enrolling_qr=enrolling_uri,
            export_dir=app_settings.export_dir,
            msg=request.query_params.get("msg"),
            # style
            style_accent=get_setting(db, "accent", "#fb6734"),
            style_accent2=get_setting(db, "accent2", "#ce3737"),
            style_bg=get_setting(db, "style_bg", "#26121b"),
            style_radius=get_setting(db, "style_radius", "10"),
            style_font=get_setting(db, "style_font", "system"),
            style_density=get_setting(db, "style_density", "comfortable"),
            style_glass=get_setting(db, "style_glass", "0"),
            style_card_opacity=get_setting(db, "style_card_opacity", "100"),
            style_sidebar=get_setting(db, "style_sidebar", "closed"),
            wallpaper_url=get_setting(db, "wallpaper_path", "") or "",
            wallpaper_blur=get_setting(db, "wallpaper_blur", "0") or "0",
            wallpaper_dark=get_setting(db, "wallpaper_dark", "40") or "40",
        ),
    )


# ---- General ----
@router.post("/general")
async def update_general(
    display_name: str = Form(""),
    hourly_rate: str = Form("0"),
    currency: str = Form("€"),
    language: str = Form(DEFAULT_LANG),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    user.display_name = display_name.strip() or None
    try:
        rate = float(hourly_rate.replace(",", ".").strip())
    except ValueError:
        rate = 0.0
    set_setting(db, "hourly_rate", str(rate))
    set_setting(db, "currency", currency.strip() or "€")
    set_setting(db, "language", language if language in LANGUAGES else DEFAULT_LANG)
    return RedirectResponse("/settings?tab=general&msg=Saved", status_code=303)


# ---- Style ----
@router.post("/style")
async def update_style(
    accent: str = Form("#6d28d9"),
    accent2: str = Form("#4f8cff"),
    bg: str = Form("#0f1115"),
    radius: str = Form("10"),
    font: str = Form("system"),
    density: str = Form("comfortable"),
    glass: str = Form(""),
    card_opacity: str = Form("100"),
    sidebar: str = Form("closed"),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    def hexcol(v, d):
        v = v.strip()
        return v if v.startswith("#") and len(v) in (4, 7) else d

    def clampi(v, lo, hi, d):
        try:
            return str(max(lo, min(hi, int(float(v)))))
        except ValueError:
            return str(d)

    set_setting(db, "accent", hexcol(accent, "#fb6734"))
    set_setting(db, "accent2", hexcol(accent2, "#ce3737"))
    set_setting(db, "style_bg", hexcol(bg, "#26121b"))
    set_setting(db, "style_radius", clampi(radius, 0, 28, 10))
    set_setting(db, "style_font", font if font in {"system", "mono", "serif", "rounded"} else "system")
    set_setting(db, "style_density", "compact" if density == "compact" else "comfortable")
    set_setting(db, "style_glass", _bx(glass))
    set_setting(db, "style_card_opacity", clampi(card_opacity, 40, 100, 100))
    set_setting(db, "style_sidebar", "open" if sidebar == "open" else "closed")
    return RedirectResponse("/settings?tab=style&msg=Saved", status_code=303)


# ---- Connections ----
@router.post("/connection/woo")
async def conn_woo(
    enabled: str = Form(""), url: str = Form(""), key: str = Form(""),
    secret: str = Form(""), order_statuses: str = Form(""),
    webhook_enabled: str = Form(""), webhook_secret: str = Form(""),
    poll_enabled: str = Form(""), poll_interval: str = Form("5"),
    task_enabled: str = Form(""), order_board: str = Form("customers"),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    try:
        interval = str(max(1, int(float(poll_interval))))
    except ValueError:
        interval = "5"
    poll_en = _bx(poll_enabled)
    runtime.save(db, {
        "woo_enabled": _bx(enabled), "woo_url": url.strip(),
        "woo_key": _secret(key, "woo_key"), "woo_secret": _secret(secret, "woo_secret"),
        "woo_order_statuses": order_statuses.strip(),
        "woo_webhook_enabled": _bx(webhook_enabled),
        "woo_webhook_secret": _secret(webhook_secret, "woo_webhook_secret"),
        "woo_poll_enabled": poll_en, "woo_poll_interval": interval,
        "woo_task_enabled": _bx(task_enabled),
        "vikunja_order_board": order_board.strip() or "customers",
    })
    # Set the watermark when polling is first enabled, so we don't retroactively
    # send receipts for all historical orders.
    if poll_en == "1" and not runtime.get("woo_poll_since"):
        runtime.save(db, {"woo_poll_since": datetime.utcnow().isoformat(timespec="seconds")})
    return RedirectResponse("/settings?tab=connections&sub=woo&msg=Saved", status_code=303)


@router.post("/connection/in")
async def conn_in(
    enabled: str = Form(""), url: str = Form(""), token: str = Form(""),
    auto_send: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    runtime.save(db, {
        "in_enabled": _bx(enabled), "in_url": url.strip(),
        "in_token": _secret(token, "in_token"), "in_auto_send": _bx(auto_send),
    })
    return RedirectResponse("/settings?tab=connections&sub=in&msg=Saved", status_code=303)


@router.post("/connection/vikunja")
async def conn_vikunja(
    enabled: str = Form(""), url: str = Form(""), token: str = Form(""),
    parent: str = Form("OpenVuture"),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    runtime.save(db, {
        "vikunja_enabled": _bx(enabled), "vikunja_url": url.strip(),
        "vikunja_token": _secret(token, "vikunja_token"),
        "vikunja_parent": parent.strip() or "OpenVuture",
    })
    return RedirectResponse("/settings?tab=connections&sub=vikunja&msg=Saved", status_code=303)


@router.post("/connection/nextcloud")
async def conn_nextcloud(
    enabled: str = Form(""), url: str = Form(""), user_name: str = Form(""),
    password: str = Form(""), base_path: str = Form("/OpenVuture/Belege"),
    auto_archive: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    runtime.save(db, {
        "nc_enabled": _bx(enabled), "nc_url": url.strip(),
        "nc_user": user_name.strip(), "nc_pass": _secret(password, "nc_pass"),
        "nc_base_path": base_path.strip() or "/OpenVuture/Belege",
        "nc_auto_archive": _bx(auto_archive),
    })
    return RedirectResponse("/settings?tab=connections&sub=nextcloud&msg=Saved", status_code=303)


@router.post("/connection/nextcloud/test")
async def conn_nextcloud_test(
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    from ..services.integrations import nextcloud

    try:
        ok = nextcloud.test_connection()
        msg = "Nextcloud-Verbindung OK." if ok else "Nextcloud-Verbindung fehlgeschlagen."
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/settings?tab=connections&sub=nextcloud&msg={msg}", status_code=303)


@router.post("/connection/ebay")
async def conn_ebay(
    enabled: str = Form(""), client_id: str = Form(""),
    client_secret: str = Form(""), marketplace: str = Form("EBAY_DE"),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    runtime.save(db, {
        "ebay_enabled": _bx(enabled), "ebay_client_id": client_id.strip(),
        "ebay_client_secret": _secret(client_secret, "ebay_client_secret"),
        "ebay_marketplace": marketplace.strip() or "EBAY_DE",
    })
    return RedirectResponse("/settings?tab=connections&sub=ebay&msg=Saved", status_code=303)


@router.post("/connection/ebay/test")
async def conn_ebay_test(
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    from ..services.integrations import ebay

    try:
        r = ebay.suggest_price("Intel Core i5")
        msg = (f"eBay OK — {r['count']} Angebote, Median {r['suggested']} {r['currency']}."
               if r.get("count") else "eBay verbunden, aber keine Angebote gefunden.")
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/settings?tab=connections&sub=ebay&msg={msg}", status_code=303)


@router.post("/connection/email")
async def conn_email(
    email_provider: str = Form("secondtrack"),
    email_enabled: str = Form(""), smtp_host: str = Form(""), smtp_port: str = Form("587"),
    smtp_user: str = Form(""), smtp_pass: str = Form(""), smtp_security: str = Form("tls"),
    mail_from_name: str = Form(""), mail_from_email: str = Form(""),
    email_auto: str = Form(""), reminder_days: str = Form("0"), dunning_days: str = Form("30"),
    tpl_invoice_subject: str = Form(""), tpl_invoice_body: str = Form(""),
    tpl_reminder_subject: str = Form(""), tpl_reminder_body: str = Form(""),
    tpl_dunning_subject: str = Form(""), tpl_dunning_body: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    def num(v, d):
        try:
            return str(int(float(v)))
        except ValueError:
            return str(d)

    runtime.save(db, {
        "email_provider": email_provider if email_provider in ("secondtrack", "invoiceninja") else "secondtrack",
        "email_enabled": _bx(email_enabled), "smtp_host": smtp_host.strip(),
        "smtp_port": num(smtp_port, 587), "smtp_user": smtp_user.strip(),
        "smtp_pass": _secret(smtp_pass, "smtp_pass"), "smtp_security": smtp_security if smtp_security in ("tls", "ssl", "none") else "tls",
        "mail_from_name": mail_from_name.strip() or "secondtrack",
        "mail_from_email": mail_from_email.strip(),
        "email_auto": _bx(email_auto), "reminder_days": num(reminder_days, 0),
        "dunning_days": num(dunning_days, 30),
        "tpl_invoice_subject": tpl_invoice_subject, "tpl_invoice_body": tpl_invoice_body,
        "tpl_reminder_subject": tpl_reminder_subject, "tpl_reminder_body": tpl_reminder_body,
        "tpl_dunning_subject": tpl_dunning_subject, "tpl_dunning_body": tpl_dunning_body,
    })
    return RedirectResponse("/settings?tab=connections&sub=email&msg=Saved", status_code=303)


@router.post("/email/test")
async def email_test(
    to: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    try:
        mailer.send_test(to.strip())
        msg = "Test email sent."
    except Exception as e:  # noqa: BLE001
        msg = f"Error: {e}"
    return RedirectResponse(f"/settings?tab=connections&sub=email&msg={msg}", status_code=303)


# ---- Wallpaper (part of Style) ----
@router.post("/wallpaper")
async def update_wallpaper(
    blur: str = Form("0"), dark: str = Form("40"),
    wallpaper: UploadFile | None = File(None),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    url = save_image(wallpaper, "wallpaper")
    if url:
        old = get_setting(db, "wallpaper_path", "")
        if old and old != url:
            delete_image(old)
        set_setting(db, "wallpaper_path", url)

    def clamp(v, lo, hi, d):
        try:
            return str(max(lo, min(hi, int(float(v)))))
        except ValueError:
            return str(d)

    set_setting(db, "wallpaper_blur", clamp(blur, 0, 40, 0))
    set_setting(db, "wallpaper_dark", clamp(dark, 0, 95, 40))
    return RedirectResponse("/settings?tab=style&msg=Background saved", status_code=303)


@router.post("/wallpaper/clear")
async def clear_wallpaper(
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    old = get_setting(db, "wallpaper_path", "")
    if old:
        delete_image(old)
    set_setting(db, "wallpaper_path", "")
    return RedirectResponse("/settings?tab=style&msg=Background removed", status_code=303)


# ---- Account: password + 2FA ----
@router.post("/password")
async def change_password(
    current: str = Form(...), new: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    if not verify_password(current, user.password_hash):
        return RedirectResponse("/settings?tab=account&msg=Current password wrong", status_code=303)
    if len(new) < 6:
        return RedirectResponse("/settings?tab=account&msg=New password too short (min. 6)", status_code=303)
    user.password_hash = hash_password(new)
    db.commit()
    return RedirectResponse("/settings?tab=account&msg=Password changed", status_code=303)


@router.post("/2fa/start")
async def start_2fa(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_login),
):
    secret = pyotp.random_base32()
    request.session["enroll_secret"] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="secondtrack")
    request.session["enroll_qr"] = _qr_data_uri(uri)
    return RedirectResponse("/settings?tab=account", status_code=303)


@router.post("/2fa/enable")
async def enable_2fa(
    request: Request, code: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    secret = request.session.get("enroll_secret")
    if not secret:
        return RedirectResponse("/settings?tab=account&msg=No 2FA setup active", status_code=303)
    if not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        request.session["enroll_qr"] = _qr_data_uri(
            pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="secondtrack")
        )
        return RedirectResponse("/settings?tab=account&msg=Code wrong, try again", status_code=303)
    user.totp_secret = secret
    user.totp_enabled = True
    db.commit()
    request.session.pop("enroll_secret", None)
    return RedirectResponse("/settings?tab=account&msg=2FA enabled", status_code=303)


@router.post("/2fa/disable")
async def disable_2fa(
    password: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    if not verify_password(password, user.password_hash):
        return RedirectResponse("/settings?tab=account&msg=Password wrong", status_code=303)
    user.totp_secret = None
    user.totp_enabled = False
    db.commit()
    return RedirectResponse("/settings?tab=account&msg=2FA disabled", status_code=303)
