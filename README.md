# secondtrack

Ein kleines, selbst gehostetes Tool, um einen Refurbishing-/Gebraucht-Hardware-
Nebenerwerb zu verfolgen: Geräte ankaufen, dran arbeiten, Teile tauschen,
ausgebaute Teile in ein virtuelles Lager legen, Arbeitszeit tracken und am Ende
den voraussichtlichen Gewinn sehen. Single-User, FastAPI + SQLite, ein Container.

## Features

- **Projekte** – Geräte in Produktion, eingelagert oder verkauft. Pro Projekt:
  verbaute Teile (mit Ein-/Verkaufspreis), Arbeitssessions (Datum, Stunden,
  Beschreibung), Stundensatz (global oder pro Projekt) und eine Zusammenfassung
  mit Vorschlagspreis, Listenpreis und Gewinn.
- **Virtuelles Lager** – ausgebaute Teile landen automatisch hier; manuell
  anlegbar. Beim Verbauen wird der hinterlegte Verkaufswert ins Projekt übernommen.
- **Statistik** – Arbeitszeit gesamt/pro Projekt, Materialausgaben, Lagerwert,
  voraussichtlicher Umsatz und Gewinn (brutto & nach Arbeitszeit).
- **Markdown-Export** – jedes Projekt als `.md` mit YAML-Frontmatter, ideal für
  Obsidian (Download-Button oder direkt in einen gemounteten Vault schreiben).
- **Login mit optionalem 2FA** (TOTP).
- **Integrationen vorbereitet** – WooCommerce & InvoiceNinja als isolierte
  Service-Module, per `.env` aktivierbar (Phase 2/3, standardmäßig aus).

## Schnellstart (Docker)

```bash
cp .env.example .env
# .env bearbeiten: SECONDTRACK_SECRET_KEY, Admin-Login etc.
docker compose up -d --build
```

Standardmäßig erreichbar unter `http://<host>:8011`. Erster Login mit den
`SECONDTRACK_ADMIN_USER`/`SECONDTRACK_ADMIN_PASSWORD` aus der `.env`
(Passwort danach in den Einstellungen änderbar).

### Reverse Proxy

Die App läuft im Container auf Port 8000 und hängt am externen Docker-Netz
`nginxpm_web`. Im Nginx Proxy Manager einen Proxy-Host auf
`secondtrack:8000` anlegen und HTTPS davorschalten – dann
`SECONDTRACK_COOKIE_SECURE=1` setzen.

### Obsidian-Export in den Vault

In `compose.yaml` das Vault-Volume einkommentieren und den Host-Pfad anpassen:

```yaml
    volumes:
      - /pfad/zu/Obsidian-Vault/secondtrack:/obsidian
```

und in der `.env`:

```
SECONDTRACK_EXPORT_DIR=/obsidian
```

Der Button „→ In Vault" auf der Projektseite schreibt dann direkt `.md`-Dateien
dorthin. Ohne Mount landen Exporte unter `/data/exports`.

## Lokal entwickeln

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SECONDTRACK_DB_PATH=./data/secondtrack.db uvicorn app.main:app --reload
```

## Konfiguration

Alle Optionen sind in [.env.example](.env.example) dokumentiert (Prefix
`SECONDTRACK_`).

## Datenmodell (Kurz)

- **Project** – Gerät: Status, Ankaufpreis, Listenpreis, optionaler Stundensatz.
- **Part** – Teil. `project_id = NULL` ⇒ es liegt im Lager. Einkaufspreis
  (optional), Verkaufswert, Herkunft (gekauft/ausgebaut).
- **WorkSession** – Arbeitssession je Projekt (Datum, Stunden, Beschreibung).
- **Setting** – UI-editierbare Einstellungen (Stundensatz, Währung).

## Hub & Integrationen

secondtrack ist das **Cockpit**, in dem alles zusammenläuft – generiert aber
selbst keine Rechnungen. Die **Rechnungs-Engine ist InvoiceNinja** (Nummernkreise,
PDF, USt, ZUGFeRD/E-Rechnung, Zahlungen, GoBD bleiben dort). secondtrack liest
beide Systeme und orchestriert nur.

```
  WooCommerce ──(Order)──┐
                         ├──► InvoiceNinja  (die eine Rechnungs-Engine)
  secondtrack ─(Projekt)─┘            │
        └──────────► Hub ◄────────────┘   (eine Übersicht + Aktionen)
```

Die **Hub-Seite** zeigt:

- KPIs aus InvoiceNinja: bezahlt, offen, Entwürfe.
- offene **WooCommerce-Bestellungen** mit Button „→ Rechnung erstellen" (legt in
  InvoiceNinja Kunde + Rechnung an) und „An Kunde senden".
- alle **InvoiceNinja-Rechnungen** mit Status, Betrag, offenem Saldo und Deep-Link.

Auf der **Projektseite** kann aus einem Projekt (Teile + Arbeitszeit) direkt eine
InvoiceNinja-Rechnung erstellt und an den Kunden gemailt werden – ideal für
Kunden-Bauaufträge.

**„An Kunde senden"** triggert InvoiceNinja, die Rechnung über dessen eigenen
SMTP zu verschicken. Mit `SECONDTRACK_INVOICENINJA_AUTO_SEND=1` passiert das
automatisch direkt beim Erstellen.

### Einrichten

1. **InvoiceNinja:** API-Token unter *Settings → Account Management → API Tokens*
   erzeugen, in `.env` `SECONDTRACK_INVOICENINJA_*` setzen, `_ENABLED=1`.
2. **WooCommerce:** unter *WooCommerce → Einstellungen → Erweitert → REST API*
   ein Schlüsselpaar (Lesezugriff genügt) anlegen, in `.env`
   `SECONDTRACK_WOO_*` setzen, `_ENABLED=1`.

Alle Calls sind in `app/services/integrations/` gekapselt; ist eine Integration
aus oder nicht erreichbar, zeigt der Hub das an statt abzustürzen.

> Hinweis Doppel-Rechnungen: secondtrack merkt sich pro Woo-Bestellung/Projekt
> die erzeugte InvoiceNinja-Rechnung (Tabelle `order_invoices`) und legt keine
> zweite an.
