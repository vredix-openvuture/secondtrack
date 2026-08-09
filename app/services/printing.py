"""Server-side label printing via CUPS.

The browser's print pipeline is what produced blank labels here: applications
that don't speak the driver's media vocabulary let it fall back to its default
page size (4x6in on the tested D520), and a 2x1in label then shows the empty
corner of a much larger page. Submitting the job ourselves with the media set
explicitly sidesteps every application print dialog — and works from any
device, because the *server* prints, not the browser.
"""
from __future__ import annotations

import subprocess

from sqlalchemy.orm import Session

from ..db import get_setting

# 144x72 PostScript points = 2x1 inch — the PPD media keyword on common
# thermal label drivers. Configurable, since other rolls/drivers differ.
DEFAULT_MEDIA = "w144h72"


def queue(db: Session) -> str:
    return (get_setting(db, "label_print_queue", "") or "").strip()


def print_pdf(db: Session, pdf: bytes, job_name: str) -> tuple[bool, str]:
    """Submit a PDF to the configured CUPS queue. Returns (ok, message)."""
    q = queue(db)
    if not q:
        return False, "Kein Etikettendrucker konfiguriert (Einstellungen → Allgemein)."
    host = (get_setting(db, "label_print_host", "") or "").strip()
    media = (get_setting(db, "label_print_media", "") or "").strip() or DEFAULT_MEDIA

    cmd = ["lp"]
    if host:
        cmd += ["-h", host]
    cmd += ["-d", q, "-t", job_name, "-o", f"media={media}", "-o", "fit-to-page", "-"]
    try:
        run = subprocess.run(
            cmd, input=pdf, capture_output=True, timeout=20,
        )
    except FileNotFoundError:
        return False, "lp fehlt im Container (cups-client nicht installiert)."
    except subprocess.TimeoutExpired:
        return False, f"Druckserver {host or 'lokal'} antwortet nicht."
    if run.returncode != 0:
        err = (run.stderr or b"").decode(errors="replace").strip()
        return False, err or f"lp exit {run.returncode}"
    return True, f"Etikett an {q} geschickt."
