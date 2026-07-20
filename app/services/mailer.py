"""SMTP mailer — all outgoing mail now goes through secondtrack itself."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .. import runtime


def is_configured() -> bool:
    return bool(
        runtime.get_bool("email_enabled")
        and runtime.get("smtp_host")
        and runtime.get("mail_from_email")
    )


def send(
    to_email: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send a plain-text email with optional attachments.
    attachments: list of (filename, data, mime_subtype e.g. 'pdf')."""
    if not is_configured():
        raise RuntimeError("Email/SMTP is not configured")
    if not to_email:
        raise RuntimeError("No recipient email address")

    msg = EmailMessage()
    from_name = runtime.get("mail_from_name") or "secondtrack"
    msg["From"] = f"{from_name} <{runtime.get('mail_from_email')}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    for fname, data, subtype in attachments or []:
        msg.add_attachment(data, maintype="application", subtype=subtype, filename=fname)

    host = runtime.get("smtp_host")
    port = runtime.get_int("smtp_port", 587)
    user = runtime.get("smtp_user")
    pw = runtime.get("smtp_pass")
    security = runtime.get("smtp_security", "tls").lower()

    if security == "ssl":
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if security == "tls":
                s.starttls(context=ssl.create_default_context())
            if user:
                s.login(user, pw)
            s.send_message(msg)


def send_test(to_email: str) -> None:
    send(to_email, "secondtrack test email",
         "This is a test email from secondtrack. SMTP is working. 🎉")
