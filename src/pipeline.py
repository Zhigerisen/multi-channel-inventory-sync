"""Run the sync engine against naive baselines, verify invariants, write the deliverables.

    python src/pipeline.py          (after generate.py)

1. Baselines: the same 60-day order stream through a scheduled snapshot sync at every interval
   in rules.engine.baselines_minutes (1440 = stock updated by hand once a day).
2. Engine run (event-time reservation + push, ledger, alerts, PO drafts, reconciliation).
3. Replay check: the whole feed delivered twice must change nothing (idempotency).
4. Invariant checks - the run fails loudly if any of them breaks.
5. out/: workbooks, full ledger and audit log, metrics.json.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time

import pandas as pd
from openpyxl.styles import Font

from common import DATA, OUT, load_rules, pct
from engine import SimResult, simulate

CANCEL_COST = (41.77, 105.63)   # cost of one cancelled order, low/high (nventory.io benchmark)


def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    master = pd.read_csv(os.path.join(DATA, "sku_master.csv"))
    listings = pd.read_csv(os.path.join(DATA, "channel_listings.csv"))
    feed = pd.read_csv(os.path.join(DATA, "orders_feed.csv"))
    return master, listings, feed


def timed(fn, *args):
    t = time.time()
    res = fn(*args)
    return res, round(time.time() - t, 1)


def run_all(master, listings, feed, rules) -> tuple[SimResult, dict, SimResult, SimResult, dict]:
    baselines, runtimes = {}, {}
    for minutes in rules["engine"]["baselines_minutes"]:
        r = copy.deepcopy(rules)
        r["engine"]["naive_sync_minutes"] = minutes
        baselines[minutes], runtimes[f"baseline_{minutes}m"] = timed(simulate, master, listings, feed, r, "naive")
        print(f"[OK] baseline sync every {minutes:>4} min: oversold orders "
              f"{baselines[minutes].metrics['oversold_orders']:,}")
    engine, runtimes["engine"] = timed(simulate, master, listings, feed, rules, "engine")
    print(f"[OK] engine: oversold orders {engine.metrics['oversold_orders']:,} "
          f"({runtimes['engine']}s for {len(feed):,} events)")
    k = rules["engine"]["stress_latency_multiplier"]
    slow = copy.deepcopy(rules)
    for cid in slow["channels"]:
        slow["channels"][cid][2] *= k
    stress, runtimes["engine_stress"] = timed(simulate, master, listings, feed, slow, "engine")
    print(f"[OK] engine stress (push latency x{k}): oversold orders {stress.metrics['oversold_orders']:,}")
    replay_feed = pd.concat([feed, feed], ignore_index=True).sort_values("ts", kind="stable")
    replay, runtimes["replay"] = timed(simulate, master, listings, replay_feed, rules, "engine")
    print(f"[OK] replay (feed delivered twice): {len(replay_feed):,} events")
    return engine, baselines, replay, stress, runtimes


def check_invariants(engine: SimResult, replay: SimResult, rules: dict) -> dict:
    m, st = engine.metrics, engine.state
    led = engine.ledger
    open_res = led[led.status == "OPEN"].groupby("sku")["qty"].sum()
    res_by_sku = st.set_index("sku")["reserved"]
    showing_ok = all(((st[f"showing_{c}"] <= st["available"]) | (st[f"showing_{c}"] < 0)).all()
                     for c in rules["channels"])
    alerts = engine.alerts.sort_values(["sku", "ts"])
    gaps = alerts.groupby("sku")["ts"].diff().dt.total_seconds().dropna()
    cooldown = rules["engine"]["alert_cooldown_hours"] * 3600
    pos = engine.pos
    checks = {
        "zero oversold orders (engine)": m["oversold_orders"] == 0,
        "ledger OPEN sum == reserved, every SKU": bool(
            (open_res.reindex(res_by_sku.index, fill_value=0) == res_by_sku).all()),
        "nightly ledger audits found 0 mismatches": m["ledger_mismatches"] == 0,
        "no negative available / reserved": bool((st["available"] >= 0).all() and (st["reserved"] >= 0).all()),
        "no channel shows more than available (end of run)": showing_ok,
        "replay of full feed changes nothing": all(
            replay.metrics[k] == m[k] for k in ("accepted_orders", "accepted_units", "oversold_orders")),
        # replay doubles every event, incl. the feed's own re-deliveries: each created order
        # is seen 2x(1 + r) times and must be reserved exactly once
        "every re-delivered order webhook ignored": replay.metrics["dup_ignored"]
        == m["orders_created"] + 2 * m["dup_ignored"],
        "PO lines only at/below reorder point": bool((pos["stock_position"] <= pos["reorder_point"]).all()),
        "PO qty never below EOQ": bool((pos["suggested_qty"] >= pos["eoq"]).all()),
        "no repeat alert inside cooldown": bool((gaps >= cooldown).all()),
    }
    for name, ok in checks.items():
        print(f"[{'OK' if ok else 'ERR'}] check: {name}")
    if not all(checks.values()):
        sys.exit("[ERR] invariant check failed - see above")
    return checks


def headline(engine: SimResult, baselines: dict, stress: SimResult, master: pd.DataFrame,
             listings: pd.DataFrame, feed: pd.DataFrame, rules: dict) -> dict:
    m = engine.metrics
    daily_b, fast_b = baselines[max(baselines)], baselines[min(baselines)]
    lat = [v[2] for v in rules["channels"].values()]
    st = engine.state.merge(master[["sku", "cost"]], on="sku")
    status = st["status"].value_counts().to_dict()
    return {
        "skus": int(len(master)), "channels": len(rules["channels"]), "days": int(rules["horizon_days"]),
        "order_events": int(len(feed)), "listings": int(len(listings)),
        "oversold_before_daily_sync": daily_b.metrics["oversold_orders"],
        "oversold_units_before_daily_sync": daily_b.metrics["oversold_units"],
        "oversold_before_15min_sync": fast_b.metrics["oversold_orders"],
        "oversold_after": m["oversold_orders"],
        "push_latency_sec": f"{min(lat):g}-{max(lat):g}",
        "stress_latency_multiplier": rules["engine"]["stress_latency_multiplier"],
        "oversold_after_stress": stress.metrics["oversold_orders"],
        "duplicate_webhooks_ignored": m["dup_ignored"],
        "extra_units_sold_vs_daily": m["accepted_units"] - daily_b.metrics["accepted_units"],
        "extra_units_sold_vs_15min": m["accepted_units"] - fast_b.metrics["accepted_units"],
        "cancel_cost_avoided_vs_daily_low": round(daily_b.metrics["oversold_orders"] * CANCEL_COST[0], 0),
        "cancel_cost_avoided_vs_daily_high": round(daily_b.metrics["oversold_orders"] * CANCEL_COST[1], 0),
        "drifts_healed": m["drifts_caught"], "hidden_units_restored": m["hidden_units_restored"],
        "pos_drafted": m["pos_drafted"], "po_lines": m["po_lines"], "po_units": m["po_units"],
        "alerts": m["alerts"], "alert_digests": m["alert_digests"],
        "suppress_events": m["suppress_events"], "relist_events": m["relist_events"],
        "status_end": {k: int(v) for k, v in status.items()},
        "inventory_value_end": round(float((st["on_hand"] * st["cost"]).sum()), 2),
    }


def comparison(engine: SimResult, baselines: dict, stress: SimResult, rules: dict) -> pd.DataFrame:
    def row(name, sync, mm):
        return {"strategy": name, "stock sync": sync,
                "oversold orders": mm["oversold_orders"], "oversold units": mm["oversold_units"],
                "units sold": mm["accepted_units"], "revenue USD": round(mm["revenue"], 2),
                "cancel cost USD (low)": round(mm["oversold_orders"] * CANCEL_COST[0], 0),
                "cancel cost USD (high)": round(mm["oversold_orders"] * CANCEL_COST[1], 0)}
    labels = {1440: "once a day (by hand)", 240: "every 4 hours", 60: "every hour", 15: "every 15 minutes"}
    rows = [row("Scheduled snapshot", labels.get(k, f"every {k} min"), b.metrics)
            for k, b in sorted(baselines.items(), reverse=True)]
    rows.append(row("This engine", "event-time reservation + push", engine.metrics))
    k = rules["engine"]["stress_latency_multiplier"]
    rows.append(row("This engine, stress test", f"pushes {k}x slower", stress.metrics))
    return pd.DataFrame(rows)


def daily_table(engine: SimResult, baselines: dict) -> pd.DataFrame:
    d = engine.daily.copy()
    out = pd.DataFrame({"date": d["date"]})
    names = {1440: "daily sync", 240: "4-hour sync", 60: "hourly sync", 15: "15-min sync"}
    for k, b in sorted(baselines.items(), reverse=True):
        out[f"oversold: {names.get(k, f'{k}-min sync')}"] = b.daily["oversold_orders"].astype(int)
    out["oversold: engine"] = d["oversold_orders"].astype(int)
    for col in ("orders", "accepted_units", "revenue", "pos", "alerts", "suppressions", "receipt_units"):
        out[f"engine {col}"] = d[col].round(2) if col == "revenue" else d[col].astype(int)
    return out


def audit_frame(engine: SimResult, rules: dict) -> pd.DataFrame:
    t0 = pd.Timestamp(rules["start_date"])
    a = pd.DataFrame(engine.audit, columns=["t", "action", "s", "field", "before", "after", "ref"])
    ts = t0 + pd.to_timedelta(a["t"], unit="s")
    sku = engine.state["sku"].to_numpy()
    return pd.DataFrame({"ts": ts.dt.strftime("%Y-%m-%d %H:%M:%S"), "cycle": "C" + ts.dt.strftime("%Y%m%d"),
                         "action": a["action"], "sku": sku[a["s"].to_numpy()], "field": a["field"],
                         "before": a["before"], "after": a["after"], "ref": a["ref"]})


def write_xlsx(path: str, sheets: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for col in ws.columns:
                width = max(len(str(col[0].value)), *(len(str(c.value)) for c in col[1:200]))
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 48)


def write_outputs(engine, baselines, stress, master, rules, checks, hl, runtimes) -> None:
    os.makedirs(OUT, exist_ok=True)
    st = master[["sku", "name", "category", "supplier_id", "lead_time_days", "cost", "price",
                 "avg_daily_demand", "safety_stock", "eoq"]].merge(engine.state, on="sku")
    write_xlsx(os.path.join(OUT, "01_Inventory_Master.xlsx"), {"Inventory (end of run)": st})
    engine.ledger.to_csv(os.path.join(OUT, "02_Reservations_Ledger.csv.gz"), index=False)
    po_head = (engine.pos.groupby(["po_id", "drafted", "supplier_id", "eta"], as_index=False)
               .agg(lines=("sku", "count"), units=("suggested_qty", "sum")))
    write_xlsx(os.path.join(OUT, "03_Purchase_Order_Drafts.xlsx"),
               {"PO headers": po_head, "PO lines": engine.pos})
    write_xlsx(os.path.join(OUT, "04_Low_Stock_Alerts.xlsx"), {"Alerts": engine.alerts})
    recon = pd.DataFrame([{"check": k, "result": "PASS" if v else "FAIL"} for k, v in checks.items()])
    write_xlsx(os.path.join(OUT, "05_Reconciliation.xlsx"),
               {"Drifts healed": engine.drifts, "Invariant checks": recon})
    audit_frame(engine, rules).to_csv(os.path.join(OUT, "06_Audit_Log.csv.gz"), index=False)
    write_xlsx(os.path.join(OUT, "07_Before_vs_After.xlsx"), {
        "Comparison": comparison(engine, baselines, stress, rules),
        "Daily": daily_table(engine, baselines),
        "Oversells prevented": baselines[max(baselines)].oversells,
    })
    metrics = {"headline": hl, "engine": engine.metrics,
               "baselines": {str(k): b.metrics for k, b in baselines.items()},
               "engine_stress": stress.metrics,
               "checks": checks, "runtime_sec": runtimes}
    with open(os.path.join(OUT, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(f"[OK] wrote {len(os.listdir(OUT))} files to out/")


def main() -> None:
    t = time.time()
    rules = load_rules()
    master, listings, feed = load_data()
    engine, baselines, replay, stress, runtimes = run_all(master, listings, feed, rules)
    checks = check_invariants(engine, replay, rules)
    hl = headline(engine, baselines, stress, master, listings, feed, rules)
    write_outputs(engine, baselines, stress, master, rules, checks, hl, runtimes)
    print(f"[OK] oversold orders: {hl['oversold_before_daily_sync']} (daily) / "
          f"{hl['oversold_before_15min_sync']} (15 min) -> {hl['oversold_after']} (engine), "
          f"{hl['oversold_after_stress']} (engine, pushes x{hl['stress_latency_multiplier']} slower)")
    print(f"[OK] units sold vs daily {hl['extra_units_sold_vs_daily']:+,} "
          f"({pct(hl['extra_units_sold_vs_daily'], baselines[max(baselines)].metrics['accepted_units'])}%), "
          f"vs 15 min {hl['extra_units_sold_vs_15min']:+,}")
    print(f"[OK] total {time.time() - t:.1f}s")


if __name__ == "__main__":
    main()
