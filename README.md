# secondtrack

A small, self-hosted tool to track a refurbishing / used-hardware side business:
buy devices, work on them, swap parts, put harvested parts into a virtual
warehouse, track working time, and see the expected profit at the end.
Single-user, FastAPI + SQLite, one container.

## Features

- **Projects** – devices in production, stored, or sold. Per project: installed
  parts (with purchase/sale price), work sessions (date, hours, description),
  hourly rate (global or per project), and a summary with suggested price, list
  price, and profit.
- **Virtual warehouse** – harvested parts land here automatically; you can also
  add them manually. When installed into a build, the stored sale value is
  carried into the project.
- **Statistics** – total/per-project working time, material expenses, warehouse
  value, expected revenue and profit (gross & after labor).
- **Markdown export** – every project as a `.md` file with YAML front matter,
  ideal for Obsidian (download button or written straight into a mounted vault).
- **Login with optional 2FA** (TOTP).
- **Integrations** – WooCommerce, InvoiceNinja, Vikunja, Nextcloud and eBay as
  isolated service modules, each toggled on via `.env` / the settings UI.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: SECONDTRACK_SECRET_KEY, admin login, etc.
docker compose up -d --build
```

Reachable by default at `http://<host>:8011`. First login uses the
`SECONDTRACK_ADMIN_USER`/`SECONDTRACK_ADMIN_PASSWORD` from `.env` (the password
can be changed in the settings afterwards).

### Reverse proxy

The app runs on port 8000 inside the container and attaches to the external
Docker network `nginxpm_web`. In the Nginx Proxy Manager, create a proxy host to
`secondtrack:8000` and put HTTPS in front of it – then set
`SECONDTRACK_COOKIE_SECURE=1`.

### Obsidian export into the vault

In `compose.yaml`, uncomment the vault volume and adjust the host path:

```yaml
    volumes:
      - /path/to/Obsidian-Vault/secondtrack:/obsidian
```

and in `.env`:

```
SECONDTRACK_EXPORT_DIR=/obsidian
```

The “→ To vault” button on the project page then writes `.md` files straight
there. Without a mount, exports land under `/data/exports`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SECONDTRACK_DB_PATH=./data/secondtrack.db uvicorn app.main:app --reload
```

## Configuration

All options are documented in [.env.example](.env.example) (prefix
`SECONDTRACK_`).

## Data model (brief)

- **Project** – container: status, customer, project number, optional hourly rate.
- **Device** – a physical device within a project: purchase/sale price, status.
- **Part** – a part. `project_id = NULL` and `device_id = NULL` ⇒ it sits in the
  warehouse. Optional purchase price, sale value, origin (purchased/harvested).
- **WorkSession** – a work session per project (date, hours, description).
- **Customer** – a customer, optionally backed by an InvoiceNinja client.
- **Setting** – UI-editable settings (hourly rate, currency).

## Hub & integrations

secondtrack is the **cockpit** where everything comes together – but it does not
generate invoices itself. The **invoicing engine is InvoiceNinja** (number
ranges, PDF, VAT, ZUGFeRD/e-invoice, payments, GoBD all stay there). secondtrack
reads both systems and only orchestrates.

```
  WooCommerce ──(order)──┐
                         ├──► InvoiceNinja  (the one invoicing engine)
  secondtrack ─(project)─┘            │
        └──────────► Hub ◄────────────┘   (one overview + actions)
```

The **Hub page** shows:

- KPIs from InvoiceNinja: paid, open, drafts.
- open **WooCommerce orders** with a “→ Create invoice” button (creates the
  customer + invoice in InvoiceNinja) and “Send to customer”.
- all **InvoiceNinja invoices** with status, amount, open balance and deep link.

On the **project page**, an InvoiceNinja invoice can be created directly from a
project (parts + working time) and emailed to the customer – ideal for
customer build jobs.

**“Send to customer”** triggers InvoiceNinja to send the invoice over its own
SMTP. With `SECONDTRACK_INVOICENINJA_AUTO_SEND=1` this happens automatically on
creation.

### Setup

1. **InvoiceNinja:** create an API token under *Settings → Account Management →
   API Tokens*, set `SECONDTRACK_INVOICENINJA_*` in `.env`, `_ENABLED=1`.
2. **WooCommerce:** under *WooCommerce → Settings → Advanced → REST API* create a
   key pair (read access is enough), set `SECONDTRACK_WOO_*` in `.env`,
   `_ENABLED=1`.

All calls are encapsulated in `app/services/integrations/`; if an integration is
off or unreachable, the Hub shows that instead of crashing.

> Note on double invoicing: secondtrack remembers the InvoiceNinja invoice
> created per Woo order/project (table `order_invoices`) and never creates a
> second one.

See also: **[GUIDE.md](GUIDE.md)** (what secondtrack does & how) and
**[SHOP-ORDERS.md](SHOP-ORDERS.md)** (the shop-order flow in detail).

> ⚠️ **Pre-alpha.** This is early, in-progress software — not production-ready.
> Expect breaking changes.
