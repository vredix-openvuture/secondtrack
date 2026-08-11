# secondtrack — Warehouse rework (design)

> As of 2026-07-27. Goal: turn the "virtual warehouse" into a **complete stock
> solution**: buy parts (new/used) → register with supplier + intake data →
> store in a physical location (rack/shelf/bin) → withdraw into a project →
> assemble a finished product → register & store the finished good (or sell it).
> Additive migration, app stays runnable at every step.

## Flow

```
Purchase (new/used) ─► Intake: supplier + purchase data
        │
        ▼
   Register part(s)  ──(lot)──► Set (components usable individually)
        │
        ▼
   Store  ─► StorageLocation (rack › shelf › bin, scan code)
        │
        ├──► Withdraw into project  (part.project_id / device_id — unchanged)
        │
        ▼
   Assemble ─► finished good = Set(kind=assembly, sellable)
        │
        ├──► sell directly (shop / invoice)
        └──► store as finished good  ─► StorageLocation
```

## Data model

### New tables

**Category** — a product category with category-specific fields.
- `id, name, icon, position, fields_json`.
- `fields_json` = ordered list of field defs, each
  `{"key","label","type","options":[],"required","unit"}`.
  `type ∈ text | number | select | bool | date`.
- A part points at a category (`Part.category_id`); its values live in
  `Part.attributes` (JSON object `{field_key: value}`). Chosen over EAV to keep
  the schema flat and lean on SQLite JSON1.

**Supplier** — `id, name, contact, email, phone, website, address, account_no,
notes`. Referenced by `Part.supplier_id`.

**StorageLocation** — hierarchical physical location.
- `id, name, code, parent_id (self FK), notes`. `.path` renders the breadcrumb.
- `code` = scan code for a printable barcode/QR label.

### Extended tables

**Part** — `category_id, supplier_id, location_id, code, attributes,
condition, serial_no, mpn, ean, warranty_until, purchase_date, min_stock,
unit, is_merch, giveaway`. `.attrs` parses `attributes`; `.low_stock` compares
`quantity` to `min_stock`.

`is_merch` puts the article in the Merch department (stickers, shirts, cases):
stocked like any part, but handed out or sold rather than built with. Without a
sale price it is promo material (`.is_promo`) and its purchase is booked to the
`advertisement` expense bucket right away. `giveaway` is set on the row booked
onto a project when it was handed over for free: that row is billed at 0, stays
out of the project's material cost, and its purchase cost shows up as the
project's advertising cost (`finance.project_items` → `ad_cost`).

**PartSet** — `kind (purchase_lot | assembly), sellable, condition, notes,
code, location_id, source_project_id`. `.is_assembly`, `.warehouse_parts`.
A finished good is a set with `kind=assembly`; `sellable` marks it ready to ship.

## Scan codes & labels

- Short unique code per object: prefix (`P` part / `S` set / `L` location) +
  7 chars from an unambiguous alphabet (no 0/O, 1/I/L). `app/services/codes.py`.
- `/s/<code>` resolves a code → redirects to the object (part/set → warehouse
  with the row focused; location → locations page focused).
- `/label/<code>` renders a printable QR label (QR encodes the absolute
  `/s/<code>` URL). Base URL: `public_base_url` setting, else `request.base_url`.
- Phone camera scans the QR → opens the object in secondtrack.

## Migration (additive)

`_ensure_columns()` adds the new part/set columns; `create_all` makes the new
tables; `_backfill_codes()` assigns codes to pre-existing parts/sets. All
idempotent, safe on every startup — matches the projects-rework pattern.

## File layout

- `models.py` — Category / Supplier / StorageLocation; Part/PartSet fields.
- `db.py` — `_ensure_columns` additions + `_backfill_codes`.
- `services/codes.py` — code generation, resolve, QR rendering.
- `services/warehouse.py` — category field parsing/validation, attribute
  extraction from forms, stock aggregation helpers.
- `routers/warehouse.py` — stock list + part CRUD (reworked).
- `routers/categories.py` — category CRUD (Settings › Categories).
- `routers/suppliers.py` — supplier CRUD (Warehouse › Suppliers).
- `routers/locations.py` — storage-location tree (Warehouse › Locations).
- `routers/scan.py` — `/s/<code>` resolve + `/label/<code>`.
- Templates: `warehouse/list.html` (reworked), `warehouse/suppliers.html`,
  `warehouse/locations.html`, `warehouse/label.html`, `settings.html`
  (Categories tab), plus `templates/warehouse/_nav.html` sub-navigation.

## Phases

- **W1** Data model + migration + code service + this doc.
- **W2** Categories + dynamic custom fields (settings + warehouse forms).
- **W3** Suppliers (CRUD + purchase-form link).
- **W4** Storage locations (hierarchy) + barcode/QR scan + labels.
- **W5** Finished goods / assemblies (sellable flag, stock-from-project).
- **W6** Richer intake fields (condition/serial/warranty/min-stock) + polish.
