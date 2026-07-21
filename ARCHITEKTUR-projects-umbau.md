# secondtrack — Projects-Umbau (Design)

> Stand: 2026-07-21. Design für #5. Ziel: Projekt ist ein **Container** (mehrere
> Geräte + Teile + Arbeit), mit Kunde und generierbarer Rechnung — nicht mehr
> „ein Projekt = ein Gerät". Wird **erst abgestimmt, dann implementiert.**

## IST (heute)

`Project` **ist** das Gerät: `name, status, kind, purchase_price, sale_price,
hourly_rate` + `parts[]` + `sessions[]`. Rechnung/Stats hängen direkt am Project.
Kunde existiert nur implizit (bei der Rechnungserstellung als IN-Client eingegeben).

## SOLL (Zielmodell)

```
Customer 1───* Project 1───* Device
                   │            └──* Part (verbaut am Gerät)
                   ├──* Part (Projekt-Teile ohne Gerätezuordnung / aus Warehouse)
                   ├──* WorkSession (Stunden)
                   └──* Report (Markdown-Berichte)
Warehouse = Part mit project_id IS NULL (unverändert)
```

### Neue/000geänderte Entitäten

**Customer** *(neu)*
- `id, name, kind` (`invoiceninja` | `internal`), `invoiceninja_client_id` (nullable),
  `email, company` (Cache fürs Anzeigen), `created_at`.
- Auswahl beim Projekt: bestehenden IN-Kunden wählen (aus `list_clients`), neu anlegen
  (→ legt auch IN-Client an, wenn nicht intern), oder **intern** (kein IN-Client).

**Project** *(umgebaut)*
- Neu: `number` (s.u.), `customer_id` (FK), `title` (statt Geräte-`name`).
- Bleibt: `status` (in_production/archived/sold → ggf. `open/in_progress/done/invoiced`),
  `hourly_rate`, `created_at`, `invoiceninja_id` (die erzeugte Rechnung).
- Entfällt am Project: `purchase_price/sale_price/kind/woo_product_id` → wandern auf **Device**.

**Device** *(neu)*
- `id, project_id, name, status, purchase_price, sale_price (optional), woo_product_id,
  image_path, created_at`. Ein Projekt hat 1..n Geräte.
- Teile werden am Gerät verbaut (`Part.device_id`), Arbeitszeit i.d.R. am Projekt.

**Part** *(erweitert)*
- Neu: `device_id` (nullable, FK). `project_id` bleibt (Warehouse = beide NULL).
- „Aus Warehouse ans Projekt/Gerät" = `project_id`/`device_id` setzen. „Neu kaufen aufs
  Projekt" = Part direkt mit project/device anlegen (+ optional Expense-Beleg).

**WorkSession** *(unverändert)* — hängt am Project.

**Report** *(neu)*
- `id, project_id, created_at, title, body_md` (Markdown, nutzt den #4-Editor).

### Projektnummer

`PJ-<ISO-Datum>-<4-stellig alnum>`, z.B. `PJ-20260721-K7F2`.
- Datum = Anlagedatum (`YYYYMMDD`).
- 4 Zeichen aus `[A-Z0-9]`, kollisionsgeprüft gegen bestehende `Project.number`.
- Generierung server-seitig beim Anlegen (nicht Math.random im Client).

## Flows

1. **Projekt anlegen:** Titel + Kunde (IN wählen / neu / intern) → `number` generiert.
2. **Geräte/Teile:** im Projekt Geräte hinzufügen; Teile aus Warehouse ziehen oder neu
   kaufen (→ optional direkt eine Expense mit Beleg, landet via #4 in Nextcloud).
3. **Arbeit/Berichte:** Stunden buchen, Reports (Markdown) schreiben.
4. **Abschließen → Rechnung:** aus Geräten (Verkaufswert) + Teilen + Arbeitszeit werden
   IN-Line-Items gebaut; **Entwurf** in InvoiceNinja anlegen. Vor dem Senden **manuell
   editierbar** (Rabatt/Position) — entweder in IN direkt (Deep-Link) oder ein kleiner
   Editor in secondtrack, der die Line-Items vor `create_invoice` anpasst.
5. **Senden:** über den bestehenden IN-Hub (`send`/`mail`).

## Migration (additiv, App bleibt lauffähig)

`_ensure_columns` + `create_all` (neue Tabellen). Phasen:
- **✅ P1 – Additiv (fertig, verifiziert):** neue Tabellen `customers, devices, reports`;
  neue Spalten `projects.number/customer_id/title`, `parts.device_id`. Status-Enum um
  `open/in_progress/done/invoiced` erweitert (Legacy-Werte bleiben). Nichts gelöscht.
- **✅ P2 – Backfill (fertig, verifiziert, idempotent):** `db._backfill_projects()` läuft
  beim Start: je Project ohne `number` → `number` erzeugen, `Device` aus den Geräte-Feldern
  klonen (Status via `in_production/archived/sold`→Device), Parts dem Device zuordnen,
  `title = name`. Kunde bleibt leer/`internal`.
- **P3 – UI-Umstellung (offen):** Projects-Liste/Detail, Create-Modal, Warehouse-Install,
  Stats/Finance auf das neue Modell; **Project.status remap** in_production→in_progress,
  archived→done, sold→invoiced. Alt-Felder am Project erst entfernen, wenn UI steht.
- **P4 – Cleanup:** verwaiste Project-Geräte-Felder entfernen (optional, spät).

## Betroffene Dateien (grob)

- `models.py` (+Customer/Device/Report, Project/Part-Felder), `db.py` (_ensure_columns + backfill)
- `services/finance.py` (compute_project über Geräte/Teile), `services/hub.py`/`invoiceninja.py`
  (line_items aus Projekt+Geräten, editierbarer Entwurf)
- `routers/projects.py` (+ devices/reports/invoice-Endpunkte), `warehouse.py` (install ans Gerät)
- Templates: `projects/list.html`, `projects/detail.html` (Geräte/Teile/Reports/Rechnung),
  Create-Modal (Kunde), `stats.html`

## Offene Entscheidungen

1. **Status-Werte** des Projekts: neues Set (`open/in_progress/done/invoiced`) oder
   bestehendes (`in_production/archived/sold`) weiternutzen?
2. **Editierbarer Rechnungsentwurf**: in secondtrack (eigener Mini-Editor) oder nur
   Deep-Link nach InvoiceNinja zum Feinschliff?
3. **Teile-Zuordnung**: Teile immer an ein Gerät, oder auch „lose" am Projekt erlaubt?
4. **Backfill**: bestehende Projekte automatisch zu 1-Geräte-Projekten migrieren — ok?
