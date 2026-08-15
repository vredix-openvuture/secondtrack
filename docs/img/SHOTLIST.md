# Screenshots

Every image the README references, what has to be on it, and how the set is taken. The README
already points at these paths, so dropping a file in here makes it appear.

## The rules for the set

- One style for all of them: the same wallpaper, the same accent colours, the same font, the same
  sidebar state. The row should read like one desktop, not like a collection.
- 600 to 1200 pixels wide. Within a row, similar aspect ratios matter more than resolution.
- The same demo data throughout. Real customer names, real addresses and real invoice numbers do
  not belong in a public repository.
- Light or dark is your call, but pick one and keep it.

## The list

| File | What is on it |
|---|---|
| `warehouse-parts.png` | The parts department: the stock value bar at the top, a filled list, one row with a category tag and a location |
| `warehouse-set.png` | The purchase-lot editor open, with the total and two or three member rows under it |
| `warehouse-merch.png` | The merch department with its stock and giveaway totals, and the handout dialog open on a project |
| `scan-label.png` | The label page for a part, QR variant, with the print and download buttons visible |
| `scan-camera.png` | The scan page with the camera running or the manual field focused |
| `warehouse-locations.png` | The location tree, at least two levels deep, with the item counts |
| `project-detail.png` | A project with its number, customer, status, and three or four assigned items |
| `project-summary.png` | The price panel: material cost, labour, suggested price, list price, profit |
| `project-report.png` | A Markdown report on a project, ideally with the slash menu open |
| `expenses-list.png` | The expense list with receipt thumbnails, the allocation column and the total |
| `hub-invoices.png` | The hub's invoice list: statuses, one overdue row, the Nextcloud sync marks |
| `stats.png` | The statistics page with the period switch on year |
| `hub-orders.png` | The hub's order section with at least one order that has an invoice and a task |
| `tasks-kanban.png` | A Kanban board with three columns and cards in each |
| `tasks-detail.png` | A task detail with labels, a due date and the project link |
| `dashboard.png` | The dashboard with six or more tiles in a deliberate arrangement |
| `settings-style.png` | Settings, Style tab, with the colour pickers and the wallpaper block |
| `settings-connections.png` | Settings, Connections, WooCommerce sub-tab, showing the webhook and polling blocks |

## Taking them

The app is behind a login, so a headless browser cannot reach the pages. Use a real window at a
fixed size and capture the region.

Open the browser at exactly the size every shot will use, so the sidebar and the cards land in the
same place every time:

```sh
chromium --app=http://localhost:40019 --window-size=1280,860 --window-position=100,100
```

Capture that window under Hyprland, either by picking it or by its geometry:

```sh
grim -g "$(slurp)" docs/img/warehouse-parts.png
grim -g "100,100 1280x860" docs/img/warehouse-parts.png
```

Bring it down to README width and strip the metadata:

```sh
magick docs/img/warehouse-parts.png -resize 1100x -strip docs/img/warehouse-parts.png
```

For a dialog that only makes sense with its backdrop, capture the whole window rather than the
dialog alone. A floating panel cut out of its context reads as a screenshot of nothing.

## Demo data

The fastest honest set comes from a throwaway database instead of doctored real one:

```sh
SECONDTRACK_DB_PATH=./data/shots.db uvicorn app.main:app --port 40019
```

The connections stay off for the warehouse, project and expense shots. The hub, order and task
shots need InvoiceNinja, WooCommerce and Vikunja pointed at test instances, since those pages show
what those systems return and nothing else.
