"""Unit tests for the sync engine on tiny hand-built scenarios.  Run:  pytest -q"""
from __future__ import annotations

import copy
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common import eoq, load_rules, reorder_point, safety_stock  # noqa: E402
from engine import simulate  # noqa: E402

START = pd.Timestamp("2026-07-01")


def make_rules(**engine) -> dict:
    r = copy.deepcopy(load_rules())
    r["horizon_days"] = 1
    r["events"]["silent_push_drop_rate"] = 0.0
    r["engine"]["publish_buffer_units"] = 0
    r["engine"].update(engine)
    return r


def make_master(rows: list[dict]) -> pd.DataFrame:
    base = {"name": "Test item", "category": "Test", "supplier_id": "SUP-001", "lead_time_days": 10,
            "cost": 5.0, "price": 10.0, "avg_daily_demand": 1.0, "sigma_daily": 0.5, "safety_stock": 0,
            "reorder_point": 0, "eoq": 0, "near_stockout_open": False, "hot_sku": False}
    return pd.DataFrame([{**base, **r} for r in rows])


def make_listings(skus: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{"channel": c, "channel_name": c, "sku": s, "channel_sku": f"{c}-{s}"}
                         for s in skus for c in ("WEB", "MKA", "MKB")])


def make_feed(events: list[tuple]) -> pd.DataFrame:
    """events: (seconds, channel, order_id, sku, qty[, cancel_after_min])"""
    rows = []
    for e in events:
        sec, ch, oid, sku, qty = e[:5]
        rows.append({"ts": (START + pd.Timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S"),
                     "channel": ch, "channel_order_id": oid, "sku": sku, "qty": qty,
                     "sim_ship_after_h": 30.0, "sim_buyer_cancel_after_min": e[5] if len(e) > 5 else np.nan,
                     "sim_return_after_d": np.nan})
    cols = ["ts", "channel", "channel_order_id", "sku", "qty", "sim_ship_after_h",
            "sim_buyer_cancel_after_min", "sim_return_after_d"]
    return pd.DataFrame(rows, columns=cols)


def test_formulas_match_textbook_values():
    assert safety_stock(np.array([2.0]), np.array([9]), 1.65)[0] == pytest.approx(9.9)
    assert reorder_point(np.array([3.0]), np.array([2.0]), np.array([9]), 1.65)[0] == 37
    assert eoq(np.array([1.0]), np.array([10.0]), 40, 0.25)[0] == 109     # sqrt(2*365*40/2.5)=108.07
    assert eoq(np.array([0.0]), np.array([10.0]), 40, 0.25)[0] == 0       # no demand -> no order


def test_cross_channel_race_oversells_on_schedule_but_not_with_engine():
    master = make_master([{"sku": "A", "on_hand_open": 3}])
    feed = make_feed([(100, "WEB", "WEB-1", "A", 2), (160, "MKA", "MKA-1", "A", 2)])
    naive = simulate(master, make_listings(["A"]), feed, make_rules(), "naive")
    engine = simulate(master, make_listings(["A"]), feed, make_rules(), "engine")
    assert naive.metrics["oversold_orders"] == 1       # MKA still showed 3 until the next sync
    assert engine.metrics["oversold_orders"] == 0      # MKA was pushed down to 1 within seconds
    assert engine.metrics["lost_orders"] == 1          # buyer saw true qty and could not order 2
    assert int(engine.state.loc[0, "reserved"]) == 2


def test_redelivered_webhook_reserves_once():
    master = make_master([{"sku": "A", "on_hand_open": 5}])
    feed = make_feed([(100, "WEB", "WEB-1", "A", 1), (200, "WEB", "WEB-1", "A", 1)])
    engine = simulate(master, make_listings(["A"]), feed, make_rules(), "engine")
    naive = simulate(master, make_listings(["A"]), feed, make_rules(), "naive")
    assert engine.metrics["dup_ignored"] == 1
    assert int(engine.state.loc[0, "reserved"]) == 1
    assert naive.metrics["phantom_reservations"] == 1
    assert int(naive.state.loc[0, "reserved"]) == 2   # stock locked by a phantom reservation


def test_low_stock_alert_is_edge_triggered_with_cooldown():
    master = make_master([{"sku": "A", "on_hand_open": 9, "reorder_point": 8}])
    feed = make_feed([(100, "WEB", "WEB-1", "A", 1, 30.0),   # dips to 8, cancelled after 30 min
                      (3000, "WEB", "WEB-2", "A", 1)])        # dips to 8 again, same day
    engine = simulate(master, make_listings(["A"]), feed, make_rules(), "engine")
    assert engine.metrics["alerts"] == 1
    assert engine.metrics["cooldown_suppressed"] == 1
    assert engine.metrics["level_alerts"] > engine.metrics["alerts"]


def test_reconciliation_heals_silently_dropped_increase():
    master = make_master([{"sku": "A", "on_hand_open": 5}])
    feed = make_feed([(100, "WEB", "WEB-1", "A", 1, 10.0)])   # cancel frees stock -> qty increase push
    rules = make_rules()
    rules["events"]["silent_push_drop_rate"] = 1.0           # every increase push is lost
    engine = simulate(master, make_listings(["A"]), feed, rules, "engine")
    assert engine.metrics["pushes_dropped"] >= 1
    assert engine.metrics["drifts_caught"] == 3              # all three channels stuck at 4
    assert engine.metrics["hidden_units_restored"] == 3
    st = engine.state.loc[0]
    assert all(st[f"showing_{c}"] == st[f"offered_{c}"] == 5 for c in ("WEB", "MKA", "MKB"))


def test_purchase_orders_grouped_by_supplier():
    master = make_master([{"sku": "A", "on_hand_open": 1, "reorder_point": 5, "eoq": 20},
                          {"sku": "B", "on_hand_open": 2, "reorder_point": 5, "eoq": 3}])
    engine = simulate(master, make_listings(["A", "B"]), make_feed([]), make_rules(), "engine")
    assert engine.metrics["pos_drafted"] == 1
    assert engine.metrics["po_lines"] == 2
    qty = dict(zip(engine.pos["sku"], engine.pos["suggested_qty"]))
    assert qty == {"A": 20, "B": 4}       # EOQ, or enough to clear the reorder point if larger
