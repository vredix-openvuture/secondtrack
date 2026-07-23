# secondtrack — Projects rework (design)

> As of 2026-07-21. Design for #5. Goal: a project is a **container** (several
> devices + parts + work), with a customer and a generatable invoice — no longer
> “one project = one device”. To be **agreed first, then implemented.**

## Current state (today)

`Project` **is** the device: `name, status, kind, purchase_price, sale_price,
hourly_rate` + `parts[]` + `sessions[]`. Invoice/stats hang directly off the
project. The customer exists only implicitly (entered as an IN client when
creating the invoice).

## Target model

```
Customer 1───* Project 1───* Device
                   │            └──* Part (installed on the device)
                   ├──* Part (project parts with no device / from the warehouse)
                   ├──* WorkSession (hours)
                   └──* Report (Markdown reports)
Warehouse = Part with project_id IS NULL (unchanged)
```

### New / changed entities

**Customer** *(new)*
- `id, name, kind` (`invoiceninja` | `internal`), `invoiceninja_client_id` (nullable),
  `email, company` (cache for display), `created_at`.
- Selection on the project: pick an existing IN customer (from `list_clients`),
  create a new one (→ also creates an IN client unless internal), or **internal**
  (no IN client).

**Project** *(reworked)*
- New: `number` (see below), `customer_id` (FK), `title` (instead of the device `name`).
- Kept: `status` (in_production/archived/sold → possibly `open/in_progress/done/invoiced`),
  `hourly_rate`, `created_at`, `invoiceninja_id` (the created invoice).
- Removed from Project: `purchase_price/sale_price/kind/woo_product_id` → move to **Device**.

**Device** *(new)*
- `id, project_id, name, status, purchase_price, sale_price (optional), woo_product_id,
  image_path, created_at`. A project has 1..n devices.
- Parts are installed on the device (`Part.device_id`), working time usually on the project.

**Part** *(extended)*
- New: `device_id` (nullable, FK). `project_id` stays (warehouse = both NULL).
- “From warehouse to project/device” = set `project_id`/`device_id`. “Buy new onto
  the project” = create the part directly with a project/device (+ optional expense receipt).

**WorkSession** *(unchanged)* — hangs off the project.

**Report** *(new)*
- `id, project_id, created_at, title, body_md` (Markdown, uses the #4 editor).

### Project number

`PJ-<ISO-date>-<4-char alnum>`, e.g. `PJ-20260721-K7F2`.
- Date = creation date (`YYYYMMDD`).
- 4 chars from `[A-Z0-9]`, collision-checked against existing `Project.number`.
- Generated server-side on creation (not Math.random in the client).

## Flows

1. **Create project:** title + customer (pick IN / new / internal) → `number` generated.
2. **Devices/parts:** add devices to the project; pull parts from the warehouse or buy
   new (→ optionally a direct expense with a receipt, filed to Nextcloud via #4).
3. **Work/reports:** log hours, write reports (Markdown).
4. **Finish → invoice:** IN line items are built from devices (sale value) + parts +
   working time; create a **draft** in InvoiceNinja. Before sending it is **editable
   manually** (discount/line) — either in IN directly (deep link) or a small editor in
   secondtrack that adjusts the line items before `create_invoice`.
5. **Send:** via the existing IN hub (`send`/`mail`).

## Migration (additive, app stays runnable)

`_ensure_columns` + `create_all` (new tables). Phases:
- **✅ P1 – Additive (done, verified):** new tables `customers, devices, reports`;
  new columns `projects.number/customer_id/title`, `parts.device_id`. Status enum extended
  with `open/in_progress/done/invoiced` (legacy values stay). Nothing deleted.
- **✅ P2 – Backfill (done, verified, idempotent):** `db._backfill_projects()` runs on
  start: for each project without a `number` → generate `number`, clone a `Device` from the
  device fields (status via `in_production/archived/sold`→Device), assign parts to the
  device, `title = name`. Customer stays empty/`internal`.
- **P3 – UI switch (in progress):**
  - **✅ P3a** – `compute_project` aggregates over `project.devices` (fallback for
    non-migrated). Numbers identical before/after migration.
  - **✅ P3b-1** – status remap (in_production→in_progress, archived→done, sold→invoiced,
    idempotent on start), finance/stats + list/detail/stats display on the new set.
  - **✅ P3b-2** – detail redesign: device section (parts nested per device), loose
    project parts, reports (Markdown); device/report CRUD; warehouse install onto the device.
  - **✅ P3c** – create modal + customer (pick existing / new: internal or IN client).
    Customer also editable in the detail header; `_resolve_customer` creates the
    InvoiceNinja client for IN customers.
  - **✅ P3d** – invoice from project: line items = device sale value + parts + work; uses
    the linked customer IN client; creates a draft in IN (deep link/send in the detail).
  - **still open (P4):** remove legacy fields on Project (`purchase_price/sale_price/
    woo_product_id`) — currently only kept as fallback/transition; the UI no longer uses them.
    **A live test against real IN/Vikunja by the user is still pending.**
- **P4 – Cleanup:** remove orphaned Project/device fields (optional, late).

## Affected files (rough)

- `models.py` (+Customer/Device/Report, Project/Part fields), `db.py` (_ensure_columns + backfill)
- `services/finance.py` (compute_project over devices/parts), `services/hub.py`/`invoiceninja.py`
  (line items from project+devices, editable draft)
- `routers/projects.py` (+ devices/reports/invoice endpoints), `warehouse.py` (install onto device)
- Templates: `projects/list.html`, `projects/detail.html` (devices/parts/reports/invoice),
  create modal (customer), `stats.html`

## Open decisions

1. **Status values** of the project: use the new set (`open/in_progress/done/invoiced`) or
   keep the existing one (`in_production/archived/sold`)?
2. **Editable invoice draft**: in secondtrack (own mini editor) or only a deep link to
   InvoiceNinja for the final touches?
3. **Part assignment**: parts always on a device, or “loose” on the project allowed too?
4. **Backfill**: automatically migrate existing projects to single-device projects — ok?
