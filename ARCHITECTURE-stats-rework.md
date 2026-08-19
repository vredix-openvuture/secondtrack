# secondtrack — Statistics rework (design)

> As of 2026-08-19. The statistics page keeps producing figures nobody can
> derive, because it answers three different questions in one list and mixes
> their sources. This is the state of it, the defects found, and what a rework
> has to settle. **Nothing here is implemented.**

## Why this document exists

The stock figure was wrong twice in a row on the same afternoon, and the second
attempt made it worse rather than better.

1. `Stock value (cost)` summed each part's **unit** purchase price once: no
   quantities, sets ignored. 88 stickers counted as one sticker. It read 57.35.
2. That was replaced by the warehouse page's own calculation, shared through
   `warehouse.stock_totals`, so all three surfaces agree. It now reads the sum
   of loose parts x quantity + lot totals + assembly costs.

Both were arithmetic fixes to a figure whose **definition** was never settled.
The second one is arithmetically defensible and still reads as nonsense next to
the other numbers on that page, because those numbers do not share a frame.
Patching the arithmetic again will not help. The page needs a definition first.

## The three frames, currently mixed

| Frame | Question | Truth lives in |
|---|---|---|
| **Inventory** | What do I own right now? | Parts, sets — a snapshot |
| **Spend** | What has left my account? | The `expenses` table, receipts, mirrored to InvoiceNinja |
| **Forecast** | What might I earn? | Project list prices, not yet real |

The page lists all three under two headings, and one figure straddles two of
them:

```
material_expenses = Σ project material_cost + warehouse_stock_cost
```

That is inventory arithmetic wearing the word "expenses". It is not what was
spent: it is what the things currently on hand are carried at. Meanwhile the
profit and loss box computes real spend from the `expenses` table. **Two
independent answers to "what did I spend", on one page, that can never agree.**

## Defects found, each verifiable

### 1. A finished good built from a project counts its cost twice

`warehouse.stock_from_project` creates the assembly with
`purchase_price = f.material_cost` while the project keeps every item it was
built from. `material_expenses` then adds the project's material cost **and**
the assembly's cost. The same purchase, twice.

### 2. Promo merch counts twice

Merch bought without a sale price books its purchase into the `advertisement`
expense bucket immediately, and the part still sits on the shelf carrying that
purchase price. It is inside `warehouse_stock_cost` and inside the advertising
figure at the same time.

### 3. Sold work never leaves the inventory frame

Items stay booked on a project after it is invoiced and paid — correctly, they
are the record of what was sold. But their cost keeps contributing to
`material_expenses` forever, so that figure only ever grows and answers nothing.

### 4. WIP is in the cost figure and not in the value figure

Defensible on its own (a half-built machine ties up material but cannot be
sold), invisible on the page. Cost and value are shown side by side as if they
described the same set of objects.

### 5. Stale counters

`Stats.archived_count` and `sold_count` predate the new lifecycle
(`payment_pending` / `paid` / `closed`). `sold_count` now counts four statuses;
the name says one.

## What the rework has to settle

These are decisions, not code. They belong to the owner of the business, not to
whoever writes the queries.

1. **Is "material expenses" spend or inventory?** If spend, it comes from the
   `expenses` table and nowhere else, and the derived figure disappears.
2. **When does a purchase stop counting?** Proposal: an item's cost belongs to
   inventory until it is sold, then to cost-of-sales for the period it sold in,
   and never to both.
3. **Does the page report a period or a moment?** Inventory is a moment, spend
   and revenue are a period. They cannot share one heading without saying so.
4. **What is the shelf worth?** Purchase cost and resale value are both
   defensible; showing both without labelling which objects each covers is what
   produced "a dumber number".

## Target shape

Three sections, each with one frame stated in its heading, and no figure that
belongs to two of them.

```
Right now            (a moment)
  Stock at cost          what is on the shelf, at what it cost
  Stock at sale value    the same objects, at what they should fetch
  Tied up in builds      WIP, listed apart so it is not mistaken for sellable
  Open work              projects not yet invoiced, at their list price

This period           (month / year / all)
  Spent                  from the expense ledger, with a receipt behind it
  Earned                 paid InvoiceNinja invoices
  Result                 the difference, the only true profit figure
  Hours logged           and what they were worth at the rate that applied

Per project           (unchanged table, it is the one part that works)
```

Everything else, especially any figure that adds a project's cost to a shelf's
cost, is dropped rather than renamed.

## Code that will need to change

| Where | What |
|---|---|
| `services/finance.py` | `Stats` loses `material_expenses`; add period-scoped spend/earn; rename the status counters |
| `services/warehouse.py` | `stock_totals` gains a WIP split, so the page can show it apart |
| `services/expenses.py` | `profit_loss` becomes the source for the period frame rather than a box at the bottom |
| `routers/stats.py` | period applies to the whole period section, not only the P&L box |
| `templates/stats.html` | three sections as above |
| `routers/warehouse.py` | `stock_from_project` must stop copying the project cost onto the assembly, or the assembly must exclude it |

## What not to repeat

- Do not fix a figure without writing down what it means first. Both attempts
  above skipped that step, which is why the second one was not better.
- Do not let two code paths compute the same business figure. That was the
  cause of defect 1 and of the original mismatch between the warehouse page and
  this one.
- Measure against a shelf with known contents. A test shelf with one lot, one
  WIP build, one finished good, one multi-unit part and one promo merch item
  exercises every branch above.
