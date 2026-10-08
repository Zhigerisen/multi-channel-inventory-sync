# Multi-Channel Inventory Sync - 10,000 SKUs, 3 channels

![cover](screens/01-cover.png)

An inventory-sync engine for sellers who list the same stock on several channels.
Every stock change is **pushed to every channel within seconds**, so a second channel stops
selling units that are already gone. Around it: reservations at order intake, webhook
de-duplication, low-stock alerts, nightly supplier purchase-order drafts, a reconciliation
sweep, an audit trail - and the results published into a Google Sheet.

> **Demo on a synthetic dataset.** 10,000 SKUs, 3 channels and a 60-day order stream are
> generated with a fixed random seed. No real brands, stores, customers or orders.
> **Channel APIs are simulated** (a qty push lands after a per-channel latency of 3-25 s);
> the engine, ledger, reorder and reconciliation logic is the real thing.

## Results - one simulated 156,057-event order stream, every strategy

Both sides share reservation at intake, webhook de-duplication, buyer behaviour, shipments,
returns and nightly PO drafting. The only difference is **how stock reaches the channels**.

| How stock is synced | Oversold orders (60 days) | Units sold |
|---|---:|---:|
| Once a day (spreadsheet updated by hand) | 239 | 193,797 |
| Every 4 hours | 61 | 194,442 |
| Every hour | 17 | 194,457 |
| Every 15 minutes | 8 | 194,460 |
| **This engine** (event-time push, 3-25 s latency) | **0** | **194,459** |
| This engine, stress test: every push 20x slower (1-8 min) | 4 | 194,459 |

What it means: the race window between a sale on one channel and the qty update on the others
shrinks from hours/minutes to seconds. Sales volume stays the same - the gain is the oversells
(cancellations, refunds, marketplace account-health hits) that no longer happen.
At a vendor-published estimate of $41.77-$105.63 per cancelled order (nventory.io), 239 oversells
are $10.0k-$25.2k over 60 days vs a daily update, $334-$845 vs a 15-minute sync.

Also in the run:
- **930** re-delivered order webhooks ignored (idempotency key = channel order id)
- **80** channel updates that "succeeded" but never landed - found and healed by the nightly sweep (2,587 units back on sale)
- **1,404** supplier PO drafts (8,749 lines, reorder point / EOQ), grouped per supplier
- **8,912** low-stock alerts in 3,701 digests - one alert per reorder-point crossing, 24h cooldown

## How the engine works

1. **Idempotent intake** - every order event is keyed by its channel order id; re-delivered webhooks are ignored.
2. **Append-only reservations ledger** - `reserved` is always the sum of open reservations (audited nightly).
3. **Available-to-sell** = on hand - reserved.
4. **Event-time push** of the new qty to every channel; pushes for the same listing are coalesced.
   In the model a decrease always lands; an increase can silently fail (0.4%) and is healed by reconciliation.
5. **Low-stock alerts** are edge-triggered (one per crossing of the reorder point) with a 24h cooldown,
   sent as one digest per cycle; a crossing held back by the cooldown fires once the cooldown ends.
6. **Nightly job**: (s, Q) reorder policy - when stock position (available + on order) <= reorder point,
   draft a line of `max(EOQ, enough to clear the reorder point)`; lines are grouped into one PO per supplier.
   Then the ledger audit and the channel reconciliation sweep run.

Formulas: `safety stock = Z x sigma_d x sqrt(L)`, `reorder point = d x L + safety stock`,
`EOQ = sqrt(2 x D x S / H)`. All parameters live in [`rules.yaml`](rules.yaml).

Optional policies in `rules.yaml`, all off: publish buffer on hot SKUs, last units only on the
fastest channel, per-channel offer cap. Measured on this dataset they cost sales without removing
further oversells, so the default keeps them off.

## Every run checks itself

`src/pipeline.py` stops with an error if any of these break: zero oversold orders for the engine,
ledger == reserved for every SKU, nightly ledger audits clean, no negative stock, no channel showing
more than available at the end of the run, replaying the full feed twice changes nothing, every
re-delivered webhook ignored, PO lines only at/below the reorder point, PO qty never below EOQ,
no repeat alert inside the cooldown.

## Run it

```bash
pip install -r requirements.txt
python src/generate.py        # synthetic catalogue, listings, 60-day order feed  -> data/
python src/pipeline.py        # baselines + engine + stress + replay + checks      -> out/
python -m pytest -q tests     # unit tests: race, idempotency, alerts, reconciliation, POs
python src/visuals.py         # slides -> screens/
```

Google Sheets (optional): share a spreadsheet with your service account, set `GOOGLE_CREDENTIALS`
to its key file, then `python src/to_gsheet.py <spreadsheet id>` and
`python src/sheet_snapshots.py <spreadsheet id>` (Google renders the tabs via PDF export - no browser).
The sheet is a published result of the run: inventory status and stock KPIs are live formulas over
the 10,000-row Inventory tab; run metrics come from the engine.

## Layout

```
rules.yaml               every parameter of data, engine and reorder policy
src/generate.py          synthetic 10k-SKU catalogue, channel SKU mapping, order feed
src/engine.py            event-driven simulation: scheduled sync vs event-time push
src/pipeline.py          runs all strategies + stress + replay, checks invariants, writes out/
src/to_gsheet.py         Google Sheet: dashboard, inventory, POs, alerts, audit
src/sheet_snapshots.py   browser-free snapshots of the sheet
src/visuals.py           portfolio slides
tests/test_engine.py     unit tests on hand-built scenarios
out/                     inventory master, reservations ledger, PO drafts, alerts,
                         reconciliation, audit log, before/after, metrics.json
```

![sheet dashboard](screens/02-live-dashboard.png)
![race](screens/03-race.png)
![before vs after](screens/04-before-after.png)
