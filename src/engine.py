"""Event-driven multi-channel inventory simulation: one order stream, two sync strategies.

mode="naive"   What most stores run today: a scheduled job pushes the SAME available qty
               to every channel every N minutes; order webhooks are not de-duplicated.
mode="engine"  This project's sync engine:
               - reservation at order intake into an append-only ledger
                 (idempotency key = channel order id, re-delivered webhooks ignored)
               - event-time push to each channel (per-channel latency, pushes coalesced)
               - publish buffer on hot SKUs, last units sold only on the fastest channel,
                 offer cap on the strictest marketplace
               - qty decreases confirmed by read-back; increases best-effort (may drop silently)
               - edge-triggered low-stock alerts with cooldown, one digest per cycle
               - nightly PO drafts (reorder point / EOQ) + reconciliation sweep that
                 checks the ledger and heals channels whose qty drifted

Both modes share stock, buyer behaviour, shipments, returns and replenishment, so the only
difference measured between them is the sync strategy.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from common import DAY, channel_table, start_dt

PUSH, CANCEL, SHIP, RETURN, RECEIPT = 0, 1, 2, 3, 4
OPEN, SHIPPED, CANCELLED, PHANTOM = 0, 1, 2, 3
STATUS = {OPEN: "OPEN", SHIPPED: "SHIPPED", CANCELLED: "CANCELLED", PHANTOM: "PHANTOM"}
METRIC_KEYS = [
    "order_events", "duplicate_events", "orders_created", "accepted_orders", "accepted_units",
    "revenue", "oversold_orders", "oversold_units", "lost_orders", "lost_units",
    "dup_ignored", "phantom_reservations", "phantom_locked_units", "cancels", "shipped_units",
    "returns", "pos_drafted", "po_lines", "po_units", "receipts", "receipt_units", "sync_runs",
    "pushes_sent", "pushes_dropped", "suppress_events", "relist_events", "alerts",
    "alert_digests", "level_alerts", "cooldown_suppressed", "drifts_caught",
    "hidden_units_restored", "ledger_mismatches",
]


@dataclass
class SimResult:
    mode: str
    metrics: dict
    daily: pd.DataFrame
    state: pd.DataFrame
    ledger: pd.DataFrame
    pos: pd.DataFrame
    alerts: pd.DataFrame
    oversells: pd.DataFrame
    drifts: pd.DataFrame
    audit: list


class Sim:
    def __init__(self, master: pd.DataFrame, listings: pd.DataFrame, feed: pd.DataFrame,
                 rules: dict, mode: str):
        if mode not in ("naive", "engine"):
            raise ValueError(f"unknown mode {mode}")
        self.mode, self.rules = mode, rules
        eng, ev = rules["engine"], rules["events"]
        chans = channel_table(rules)
        self.cids = [c[0] for c in chans]
        self.C = len(chans)
        self.lat = np.array([c[3] for c in chans], dtype=float)
        self.cap = np.array([c[4] for c in chans], dtype=float)
        self.fast = int(np.argmin(self.lat))
        self.days = int(rules["horizon_days"])
        self.end = self.days * DAY
        self.rng = np.random.default_rng(int(rules["seed"]) + 7)
        self.drop_rate = float(ev["silent_push_drop_rate"])
        self.cooldown = float(eng["alert_cooldown_hours"]) * 3600
        self.tick_every = 60.0 * (eng["naive_sync_minutes"] if mode == "naive" else eng["alert_digest_minutes"])
        self.nightly_at = float(eng["nightly_run_hour"]) * 3600
        self.last_units = int(eng["last_units_fast_channel_only"])

        self.skus = master["sku"].to_numpy()
        self.n = n = len(self.skus)
        idx = pd.Series(np.arange(n), index=self.skus)
        self.on_hand = master["on_hand_open"].to_numpy(np.int64).copy()
        self.reserved = np.zeros(n, np.int64)
        self.on_order = np.zeros(n, np.int64)
        self.rop = master["reorder_point"].to_numpy(np.int64)
        self.eoq = master["eoq"].to_numpy(np.int64)
        self.lead = master["lead_time_days"].to_numpy(np.int64)
        self.price = master["price"].to_numpy(float)
        self.supplier = master["supplier_id"].to_numpy()
        self.mover = master["avg_daily_demand"].to_numpy(float) > 0
        self.buf = np.where(master["hot_sku"].to_numpy(bool), int(eng["publish_buffer_units"]), 0)

        cpos = pd.Series(np.arange(self.C), index=self.cids)
        self.listed = np.zeros((self.C, n), dtype=bool)
        self.listed[cpos[listings["channel"]].to_numpy(), idx[listings["sku"]].to_numpy()] = True

        t0 = pd.Timestamp(start_dt(rules))
        self.f_t = (pd.to_datetime(feed["ts"]) - t0).dt.total_seconds().to_numpy(float)
        self.f_c = cpos[feed["channel"]].to_numpy(np.int64)
        self.f_s = idx[feed["sku"]].to_numpy(np.int64)
        self.f_q = feed["qty"].to_numpy(np.int64)
        self.f_key = feed["channel_order_id"].to_numpy()
        self.f_ship = feed["sim_ship_after_h"].to_numpy(float) * 3600
        self.f_cancel = feed["sim_buyer_cancel_after_min"].to_numpy(float) * 60
        self.f_ret = feed["sim_return_after_d"].to_numpy(float) * DAY

        m = len(feed)
        self.led_c = np.zeros(m, np.int64)
        self.led_s = np.zeros(m, np.int64)
        self.led_q = np.zeros(m, np.int64)
        self.led_t = np.zeros(m, float)
        self.led_close = np.full(m, np.nan)
        self.led_st = np.zeros(m, np.int8)
        self.led_ret = np.full(m, np.nan)
        self.led_key: list[str] = []
        self.nres = 0

        self.heap: list = []
        self.seq = 0
        self.seen: set = set()
        self.created: set = set()
        self.pending = np.zeros((self.C, n), dtype=bool)
        self.was_below = np.zeros(n, dtype=bool)
        self.last_alert = np.full(n, -1e18)
        self.m = dict.fromkeys(METRIC_KEYS, 0)
        self.daily = {k: np.zeros(self.days) for k in
                      ("orders", "accepted_units", "revenue", "oversold_orders", "oversold_units",
                       "lost_units", "pos", "alerts", "suppressions", "receipt_units")}
        self.pos, self.alerts, self.oversells, self.drifts, self.audit = [], [], [], [], []

        if mode == "engine":
            self.shown = self.offers_all()
            self.hub_view = self.shown.copy()
        else:
            self.shown = self.listed * np.maximum(self.on_hand - self.reserved, 0)[None, :]
            self.hub_view = self.shown.copy()

    # ---------------- helpers ----------------
    def push(self, t: float, kind: int, a: int, b: int = 0) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (t, kind, self.seq, a, b))

    def log(self, t: float, action: str, s: int, field: str, before: int, after: int, ref: str) -> None:
        if self.mode == "engine":
            self.audit.append((t, action, s, field, before, after, ref))

    def day(self, t: float) -> int:
        return min(int(t // DAY), self.days - 1)

    def offers(self, s: int) -> list[int]:
        ats = int(self.on_hand[s] - self.reserved[s] - self.buf[s])
        res = [0] * self.C
        if ats <= 0:
            return res
        for c in range(self.C):
            if self.listed[c, s] and (ats > self.last_units or c == self.fast):
                res[c] = int(ats * self.cap[c])
        return res

    def offers_all(self) -> np.ndarray:
        ats = np.maximum(self.on_hand - self.reserved - self.buf, 0)
        off = np.floor(ats[None, :] * self.cap[:, None]).astype(np.int64) * self.listed
        slow = np.arange(self.C) != self.fast
        off[np.ix_(slow, ats <= self.last_units)] = 0
        return off

    def sync_sku(self, s: int, t: float) -> None:
        """Engine only: schedule (coalesced) event-time pushes for every channel whose qty is stale."""
        if self.mode != "engine":
            return
        off = self.offers(s)
        for c in range(self.C):
            if self.listed[c, s] and not self.pending[c, s] and off[c] != self.hub_view[c, s]:
                self.pending[c, s] = True
                self.push(t + self.lat[c] * self.rng.uniform(0.7, 1.3), PUSH, c, s)

    def reserve(self, t: float, c: int, s: int, q: int, key: str, status: int) -> int:
        r = self.nres
        self.nres += 1
        self.led_c[r], self.led_s[r], self.led_q[r], self.led_t[r], self.led_st[r] = c, s, q, t, status
        self.led_key.append(key)
        before = int(self.reserved[s])
        self.reserved[s] += q
        self.log(t, "RESERVE", s, "reserved", before, before + q, key)
        return r

    # ---------------- event handlers ----------------
    def handle_order(self, i: int) -> None:
        t, c, s, q, key = self.f_t[i], int(self.f_c[i]), int(self.f_s[i]), int(self.f_q[i]), self.f_key[i]
        self.m["order_events"] += 1
        if key in self.seen:
            self.m["duplicate_events"] += 1
            if key in self.created:
                self.on_duplicate(t, c, s, q, key)
            return
        self.seen.add(key)
        d = self.day(t)
        if q > self.shown[c, s]:
            self.m["lost_orders"] += 1
            self.m["lost_units"] += q
            self.daily["lost_units"][d] += q
            return
        self.created.add(key)
        self.shown[c, s] -= q
        self.hub_view[c, s] -= q
        self.m["orders_created"] += 1
        self.daily["orders"][d] += 1
        avail = int(self.on_hand[s] - self.reserved[s])
        if q > avail:
            self.m["oversold_orders"] += 1
            self.m["oversold_units"] += q
            self.daily["oversold_orders"][d] += 1
            self.daily["oversold_units"][d] += q
            self.oversells.append((t, c, s, q, avail))
            self.sync_sku(s, t)
            return
        r = self.reserve(t, c, s, q, key, OPEN)
        self.led_ret[r] = self.f_ret[i]
        if not math.isnan(self.f_cancel[i]):
            self.push(t + self.f_cancel[i], CANCEL, r)
        else:
            self.push(t + self.f_ship[i], SHIP, r)
        self.m["accepted_orders"] += 1
        self.m["accepted_units"] += q
        self.m["revenue"] += q * self.price[s]
        self.daily["accepted_units"][d] += q
        self.daily["revenue"][d] += q * self.price[s]
        self.sync_sku(s, t)

    def on_duplicate(self, t: float, c: int, s: int, q: int, key: str) -> None:
        if self.mode == "engine":
            self.m["dup_ignored"] += 1
            self.log(t, "DUPLICATE_IGNORED", s, "reserved", int(self.reserved[s]), int(self.reserved[s]), key)
            return
        if q <= self.on_hand[s] - self.reserved[s]:
            self.reserve(t, c, s, q, key, PHANTOM)
            self.m["phantom_reservations"] += 1
            self.m["phantom_locked_units"] += q

    def on_push(self, t: float, c: int, s: int) -> None:
        self.pending[c, s] = False
        val = self.offers(s)[c]
        self.m["pushes_sent"] += 1
        if val > self.hub_view[c, s] and self.rng.random() < self.drop_rate:
            self.hub_view[c, s] = val
            self.m["pushes_dropped"] += 1
            return
        before = int(self.shown[c, s])
        self.shown[c, s] = val
        self.hub_view[c, s] = val
        if before > 0 and val == 0:
            self.m["suppress_events"] += 1
            self.daily["suppressions"][self.day(t)] += 1
            self.log(t, "LISTING_SUPPRESSED", s, f"qty@{self.cids[c]}", before, 0, self.cids[c])
        elif before == 0 and val > 0:
            self.m["relist_events"] += 1
            self.log(t, "LISTING_RELISTED", s, f"qty@{self.cids[c]}", 0, val, self.cids[c])

    def on_cancel(self, t: float, r: int) -> None:
        if self.led_st[r] != OPEN:
            return
        s, q = int(self.led_s[r]), int(self.led_q[r])
        before = int(self.reserved[s])
        self.reserved[s] -= q
        self.led_st[r], self.led_close[r] = CANCELLED, t
        self.m["cancels"] += 1
        self.log(t, "BUYER_CANCEL", s, "reserved", before, before - q, self.led_key[r])
        self.sync_sku(s, t)

    def on_ship(self, t: float, r: int) -> None:
        if self.led_st[r] != OPEN:
            return
        s, q = int(self.led_s[r]), int(self.led_q[r])
        before = int(self.on_hand[s])
        self.on_hand[s] -= q
        self.reserved[s] -= q
        self.led_st[r], self.led_close[r] = SHIPPED, t
        self.m["shipped_units"] += q
        self.log(t, "SHIP", s, "on_hand", before, before - q, self.led_key[r])
        if not math.isnan(self.led_ret[r]):
            self.push(t + self.led_ret[r], RETURN, r)

    def on_return(self, t: float, r: int) -> None:
        s, q = int(self.led_s[r]), int(self.led_q[r])
        before = int(self.on_hand[s])
        self.on_hand[s] += q
        self.m["returns"] += 1
        self.log(t, "RETURN_RESTOCK", s, "on_hand", before, before + q, self.led_key[r])
        self.sync_sku(s, t)

    def on_receipt(self, t: float, p: int) -> None:
        po = self.pos[p]
        self.m["receipts"] += 1
        for s, qty, *_ in po["lines"]:
            before = int(self.on_hand[s])
            self.on_hand[s] += qty
            self.on_order[s] -= qty
            self.m["receipt_units"] += qty
            self.daily["receipt_units"][self.day(t)] += qty
            self.log(t, "PO_RECEIVED", s, "on_hand", before, before + qty, po["po_id"])
            self.sync_sku(s, t)

    # ---------------- scheduled jobs ----------------
    def on_tick(self, t: float) -> None:
        if self.mode == "naive":
            self.m["sync_runs"] += 1
            self.m["pushes_sent"] += int(self.listed.sum())
            self.shown = self.listed * np.maximum(self.on_hand - self.reserved, 0)[None, :]
            self.hub_view = self.shown.copy()
            return
        ats = self.on_hand - self.reserved
        below = (ats <= self.rop) & self.mover
        cross = below & ~self.was_below
        ok = cross & (t - self.last_alert >= self.cooldown)
        self.m["level_alerts"] += int(below.sum())
        self.m["cooldown_suppressed"] += int((cross & ~ok).sum())
        hits = np.flatnonzero(ok)
        if hits.size:
            self.m["alerts"] += int(hits.size)
            self.m["alert_digests"] += 1
            self.daily["alerts"][self.day(t)] += hits.size
            self.last_alert[hits] = t
            for s in hits:
                self.alerts.append((t, int(s), int(ats[s]), int(self.rop[s]), int(self.on_order[s])))
        self.was_below = below

    def on_nightly(self, t: float) -> None:
        """(s, Q) policy: when stock position (available + on order) is at/below the reorder
        point, draft a line; lines are grouped into one purchase order per supplier."""
        position = self.on_hand - self.reserved + self.on_order
        need = np.flatnonzero((position <= self.rop) & self.mover)
        by_supplier: dict[str, list[int]] = {}
        for s in need:
            by_supplier.setdefault(self.supplier[s], []).append(int(s))
        for sup, sku_idx in sorted(by_supplier.items()):
            p = len(self.pos)
            po_id = f"PO-{p + 1:05d}"
            lines = []
            for s in sku_idx:
                qty = int(max(self.eoq[s], self.rop[s] - position[s] + 1))
                lines.append((s, qty, int(position[s]), int(self.rop[s]), int(self.eoq[s])))
                self.on_order[s] += qty
                self.m["po_lines"] += 1
                self.m["po_units"] += qty
                self.log(t, "PO_LINE_DRAFTED", s, "on_order", 0, qty, po_id)
            eta = t + self.lead[sku_idx[0]] * DAY
            self.pos.append({"po_id": po_id, "t": t, "supplier": sup, "lines": lines, "eta": eta})
            self.m["pos_drafted"] += 1
            self.daily["pos"][self.day(t)] += 1
            self.push(eta, RECEIPT, p)
        if self.mode == "engine":
            self.reconcile(t)

    def reconcile(self, t: float) -> None:
        r = self.nres
        open_ = self.led_st[:r] == OPEN
        from_ledger = np.bincount(self.led_s[:r][open_], weights=self.led_q[:r][open_],
                                  minlength=self.n).astype(np.int64)
        self.m["ledger_mismatches"] += int((from_ledger != self.reserved).sum())
        drift = self.listed & (self.shown != self.hub_view) & ~self.pending
        for c, s in zip(*np.nonzero(drift)):
            shown, expected = int(self.shown[c, s]), int(self.hub_view[c, s])
            self.drifts.append((t, int(c), int(s), shown, expected))
            self.m["drifts_caught"] += 1
            self.m["hidden_units_restored"] += max(expected - shown, 0)
            self.shown[c, s] = expected
            self.log(t, "DRIFT_HEALED", int(s), f"qty@{self.cids[c]}", shown, expected, "reconciliation")

    # ---------------- main loop ----------------
    def run(self) -> SimResult:
        i, nfeed = 0, len(self.f_t)
        next_tick, next_night = 0.0, self.nightly_at
        handlers = {PUSH: lambda t, a, b: self.on_push(t, a, b),
                    CANCEL: lambda t, a, b: self.on_cancel(t, a),
                    SHIP: lambda t, a, b: self.on_ship(t, a),
                    RETURN: lambda t, a, b: self.on_return(t, a),
                    RECEIPT: lambda t, a, b: self.on_receipt(t, a)}
        while True:
            t_feed = self.f_t[i] if i < nfeed else math.inf
            t_heap = self.heap[0][0] if self.heap else math.inf
            t = min(t_feed, t_heap, next_tick, next_night)
            if t >= self.end:
                break
            if t == next_night:
                self.on_nightly(t)
                next_night += DAY
            elif t == next_tick:
                self.on_tick(t)
                next_tick += self.tick_every
            elif t_heap <= t_feed:
                tt, kind, _, a, b = heapq.heappop(self.heap)
                handlers[kind](tt, a, b)
            else:
                self.handle_order(i)
                i += 1
        return self.result()

    def result(self) -> SimResult:
        t0 = pd.Timestamp(start_dt(self.rules))
        r = self.nres
        sku = self.skus
        ledger = pd.DataFrame({
            "reservation_id": [f"R{k + 1:07d}" for k in range(r)],
            "idempotency_key": self.led_key,
            "channel": np.array(self.cids)[self.led_c[:r]],
            "sku": sku[self.led_s[:r]], "qty": self.led_q[:r],
            "opened": t0 + pd.to_timedelta(self.led_t[:r], unit="s"),
            "status": [STATUS[int(x)] for x in self.led_st[:r]],
            "closed": t0 + pd.to_timedelta(self.led_close[:r], unit="s"),
        })
        pos = pd.DataFrame([{
            "po_id": p["po_id"], "drafted": t0 + pd.Timedelta(seconds=p["t"]),
            "supplier_id": p["supplier"], "sku": sku[s], "stock_position": pos_, "reorder_point": rop,
            "eoq": q_eoq, "suggested_qty": qty, "eta": t0 + pd.Timedelta(seconds=p["eta"])}
            for p in self.pos for s, qty, pos_, rop, q_eoq in p["lines"]])
        alerts = pd.DataFrame(self.alerts, columns=["t", "s", "available", "reorder_point", "on_order"])
        oversells = pd.DataFrame(self.oversells, columns=["t", "c", "s", "qty", "available_at_intake"])
        drifts = pd.DataFrame(self.drifts, columns=["t", "c", "s", "channel_showed", "hub_expected"])
        for df in (alerts, oversells, drifts):
            df.insert(0, "ts", t0 + pd.to_timedelta(df.pop("t"), unit="s"))
            df.insert(1, "sku", sku[df.pop("s").to_numpy(np.int64)] if len(df) else [])
        for df in (oversells, drifts):
            df.insert(1, "channel", np.array(self.cids)[df.pop("c").to_numpy(np.int64)] if len(df) else [])

        offered = self.offers_all() if self.mode == "engine" else self.shown
        avail = self.on_hand - self.reserved
        position = avail + self.on_order
        status = np.where(avail <= 0, "OUT",
                          np.where(position <= self.rop, "REORDER",
                                   np.where(avail <= self.rop, "LOW", "ACTIVE")))
        state = pd.DataFrame({"sku": sku, "on_hand": self.on_hand, "reserved": self.reserved,
                              "available": avail, "on_order": self.on_order,
                              "reorder_point": self.rop, "status": status})
        for c, cid in enumerate(self.cids):
            state[f"offered_{cid}"] = np.where(self.listed[c], offered[c], -1)
            state[f"showing_{cid}"] = np.where(self.listed[c], self.shown[c], -1)

        days = pd.date_range(t0, periods=self.days, freq="D")
        daily = pd.DataFrame({"date": days.date, **{k: v for k, v in self.daily.items()}})
        metrics = {k: (round(v, 2) if isinstance(v, float) else int(v)) for k, v in self.m.items()}
        return SimResult(self.mode, metrics, daily, state, ledger, pos, alerts, oversells, drifts, self.audit)


def simulate(master: pd.DataFrame, listings: pd.DataFrame, feed: pd.DataFrame,
             rules: dict, mode: str) -> SimResult:
    return Sim(master, listings, feed, rules, mode).run()
