"""Generate the synthetic dataset for the inventory-sync demo.

    python src/generate.py

Writes
    data/sku_master.csv        10,000 SKUs: catalogue, cost/price, planning params, opening stock
    data/channel_listings.csv  SKU x channel mapping (each channel has its own SKU code)
    data/orders_feed.csv       60-day order stream across 3 channels (incl. webhook re-deliveries)

Everything is synthetic (fixed seed): no real brands, stores or people.
Columns prefixed sim_ are simulation inputs (what the warehouse / buyer will do later).
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

from common import DATA, channel_table, eoq, load_rules, reorder_point, safety_stock, start_dt

PREFIX = {
    "Home & Kitchen": "HOM", "Beauty & Personal": "BEA", "Sports & Outdoors": "SPO",
    "Toys & Games": "TOY", "Pet Supplies": "PET", "Tech Accessories": "TEC",
    "Office & Stationery": "OFF", "Apparel Basics": "APP", "Garden & Patio": "GAR",
    "Health & Wellness": "HEA",
}
NOUNS = {
    "Home & Kitchen": ["Storage Jar", "Cutting Board", "Mixing Bowl", "Spice Rack", "Tea Towel Set",
                       "Table Lamp", "Knife Block", "Glass Carafe", "Dish Rack", "Throw Pillow"],
    "Beauty & Personal": ["Makeup Brush Set", "Hair Clip Pack", "Bamboo Hairbrush", "Travel Bottle Kit",
                          "Silk Scrunchie", "Nail Care Kit", "Cosmetic Pouch", "Face Roller"],
    "Sports & Outdoors": ["Yoga Mat", "Resistance Band Set", "Water Bottle", "Jump Rope", "Camping Lantern",
                          "Dry Bag", "Foam Roller", "Trekking Pole"],
    "Toys & Games": ["Wooden Puzzle", "Building Block Set", "Card Game", "Plush Toy", "Kite",
                     "Stacking Rings", "Marble Run", "Craft Kit"],
    "Pet Supplies": ["Dog Leash", "Cat Scratcher", "Pet Bowl", "Chew Toy", "Pet Bed", "Grooming Brush",
                     "Treat Pouch", "Litter Mat"],
    "Tech Accessories": ["USB-C Cable", "Phone Stand", "Laptop Sleeve", "Cable Organizer", "Wireless Charger",
                         "Screen Cleaner Kit", "Mouse Pad", "Power Strip"],
    "Office & Stationery": ["Notebook", "Gel Pen Set", "Desk Organizer", "Sticky Notes", "Binder Clips",
                            "Planner", "Highlighter Set", "Letter Tray"],
    "Apparel Basics": ["Crew Socks 3-Pack", "Cotton Tee", "Beanie", "Tote Bag", "Hoodie", "Bucket Hat",
                       "Ankle Socks 5-Pack", "Scarf"],
    "Garden & Patio": ["Planter Pot", "Garden Gloves", "Watering Can", "Seed Tray", "Solar Path Light",
                       "Hose Nozzle", "Pruning Shears"],
    "Health & Wellness": ["Pill Organizer", "Massage Ball", "Hot Water Bottle", "Sleep Mask",
                          "Posture Cushion", "Acupressure Mat"],
}
ADJ = ["Classic", "Compact", "Deluxe", "Everyday", "Pro", "Eco", "Mini", "Large", "Premium", "Essential"]
COLORS = ["Black", "White", "Sage", "Navy", "Oak", "Grey", "Sand", "Terracotta", "Blush", "Olive"]


def build_catalog(rules: dict, rng: np.random.Generator) -> pd.DataFrame:
    cats = list(rules["catalog"])
    spec = np.array([rules["catalog"][c] for c in cats], dtype=float)
    n = int(rules["skus"])
    ci = rng.choice(len(cats), size=n, p=spec[:, 0] / spec[:, 0].sum())

    price = np.floor(np.clip(np.exp(rng.normal(spec[ci, 1], spec[ci, 2])), 4, 400)) + 0.99
    margin = rng.uniform(spec[ci, 3], spec[ci, 4])
    cost = np.round(price * (1 - margin), 2)

    sku = np.empty(n, dtype=object)
    name = np.empty(n, dtype=object)
    for k, cat in enumerate(cats):
        idx = np.flatnonzero(ci == k)
        codes = rng.permutation(np.arange(10000, 100000))[: len(idx)]
        sku[idx] = [f"{PREFIX[cat]}-{c}" for c in codes]
        nouns = NOUNS[cat]
        name[idx] = [f"{ADJ[a]} {nouns[b]}, {COLORS[c]}" for a, b, c in zip(
            rng.integers(0, len(ADJ), len(idx)), rng.integers(0, len(nouns), len(idx)),
            rng.integers(0, len(COLORS), len(idx)))]

    n_sup = int(rules["suppliers"])
    lo, hi = rules["lead_time_days"]
    sup_lead = rng.integers(lo, hi + 1, n_sup)
    sup = rng.integers(0, n_sup, n)
    return pd.DataFrame({
        "sku": sku, "name": name, "category": np.array(cats, dtype=object)[ci],
        "supplier_id": [f"SUP-{s + 1:03d}" for s in sup], "lead_time_days": sup_lead[sup],
        "cost": cost, "price": price,
    })


def popularity(rules: dict, rng: np.random.Generator, n: int) -> np.ndarray:
    """Expected orders/day per SKU: Pareto-skewed, capped so one SKU cannot swallow the catalogue."""
    d = rules["demand"]
    w = rng.pareto(d["pareto_alpha"], n) + 1.0
    lam = d["total_daily_orders"] * w / w.sum()
    for _ in range(4):
        lam = np.minimum(lam, 45.0)
        lam *= d["total_daily_orders"] / lam.sum()
    return lam


def daily_orders(lam: np.ndarray, days: int, rules: dict, rng: np.random.Generator) -> np.ndarray:
    """Gamma-Poisson (negative binomial) daily order counts, shape (days, n)."""
    k = float(rules["demand"]["burst_shape"])
    return rng.poisson(lam * rng.gamma(k, 1.0 / k, size=(days, lam.size)))


def planning_params(cat: pd.DataFrame, lam: np.ndarray, rules: dict, rng: np.random.Generator) -> pd.DataFrame:
    d, r = rules["demand"], rules["reorder"]
    counts = daily_orders(lam, int(rules["history_days"]), rules, rng)
    extra = rng.negative_binomial(np.maximum(counts, 1), d["qty_geometric_p"])
    units = counts + np.where(counts > 0, extra, 0)
    avg = units.mean(axis=0)
    sigma = units.std(axis=0, ddof=1)
    lead = cat["lead_time_days"].to_numpy()
    z = float(r["service_level_z"])

    out = cat.copy()
    out["avg_daily_demand"] = np.round(avg, 2)
    out["sigma_daily"] = np.round(sigma, 2)
    out["safety_stock"] = np.ceil(safety_stock(sigma, lead, z)).astype(int)
    out["reorder_point"] = reorder_point(avg, sigma, lead, z)
    out["eoq"] = eoq(avg, cat["cost"].to_numpy(), r["order_cost_usd"], r["holding_rate"])

    lo, hi = r["start_cover_days"]
    on_hand = np.ceil(rng.uniform(lo, hi, len(out)) * avg + out["safety_stock"]) + rng.integers(0, 4, len(out))
    movers = np.flatnonzero(avg >= 0.5)
    near = rng.choice(movers, size=int(r["near_stockout_share"] * len(out)), replace=False)
    on_hand[near] = np.floor(out["reorder_point"].to_numpy()[near] * rng.uniform(0.5, 0.95, near.size))
    out["on_hand_open"] = on_hand.astype(int)
    out["near_stockout_open"] = False
    out.loc[near, "near_stockout_open"] = True
    out["hot_sku"] = avg >= float(rules["engine"]["hot_sku_daily_units"])
    return out


def build_listings(master: pd.DataFrame, rules: dict, rng: np.random.Generator) -> pd.DataFrame:
    """WEB lists everything; marketplaces list a subset, each with its own SKU code (SKU mapping)."""
    n = len(master)
    coverage = {"WEB": 1.0, "MKA": 0.85, "MKB": 0.60}
    rows = []
    for cid, cname, *_ in channel_table(rules):
        listed = np.flatnonzero(rng.random(n) < coverage[cid])
        if cid == "WEB":
            codes = master["sku"].to_numpy()[listed]
        else:
            nums = rng.permutation(np.arange(10_000_000, 99_999_999, 7))[: listed.size]
            codes = [f"{cid[-1]}{x}" for x in nums]
        rows.append(pd.DataFrame({"channel": cid, "channel_name": cname,
                                  "sku": master["sku"].to_numpy()[listed], "channel_sku": codes}))
    return pd.concat(rows, ignore_index=True)


def build_feed(master: pd.DataFrame, listings: pd.DataFrame, lam: np.ndarray,
               rules: dict, rng: np.random.Generator) -> pd.DataFrame:
    d, ev = rules["demand"], rules["events"]
    chans = channel_table(rules)
    days = int(rules["horizon_days"])
    counts = daily_orders(lam, days, rules, rng)
    day_i, sku_i = np.nonzero(counts)
    reps = counts[day_i, sku_i]
    day = np.repeat(day_i, reps)
    sku = np.repeat(sku_i, reps)
    m = sku.size

    hour_p = np.asarray(d["hour_profile"], dtype=float)
    hour = rng.choice(24, size=m, p=hour_p / hour_p.sum())
    sec = day * 86400 + hour * 3600 + rng.integers(0, 3600, m)
    qty = np.minimum(rng.geometric(d["qty_geometric_p"], m), int(d["max_qty"]))

    pos = {s: i for i, s in enumerate(master["sku"])}
    listed = np.zeros((len(master), len(chans)), dtype=bool)
    for c, (cid, *_rest) in enumerate(chans):
        listed[[pos[s] for s in listings.loc[listings.channel == cid, "sku"]], c] = True
    prob = listed[sku] * np.array([c[2] for c in chans])
    prob /= prob.sum(axis=1, keepdims=True)
    ch = (rng.random(m)[:, None] > np.cumsum(prob, axis=1)).sum(axis=1)

    lo_h, hi_h = ev["ship_after_hours"]
    ship_h = np.round(rng.uniform(lo_h, hi_h, m), 2)
    cancel = rng.random(m) < ev["cancel_rate"]
    cancel_min = np.where(cancel, np.round(rng.uniform(5, lo_h * 60 * 0.9, m), 1), np.nan)
    lo_d, hi_d = ev["return_after_days"]
    ret = (~cancel) & (rng.random(m) < ev["return_rate"])
    ret_d = np.where(ret, np.round(rng.uniform(lo_d, hi_d, m), 2), np.nan)

    feed = pd.DataFrame({"sec": sec, "channel": np.array([c[0] for c in chans])[ch],
                         "sku": master["sku"].to_numpy()[sku], "qty": qty,
                         "sim_ship_after_h": ship_h, "sim_buyer_cancel_after_min": cancel_min,
                         "sim_return_after_d": ret_d})
    feed = feed.sort_values("sec", kind="stable").reset_index(drop=True)
    feed["channel_order_id"] = feed.groupby("channel").cumcount().map(lambda k: f"{k + 1:07d}")
    feed["channel_order_id"] = feed["channel"] + "-" + feed["channel_order_id"]
    feed["redelivery"] = 0

    dup = feed[rng.random(len(feed)) < ev["duplicate_webhook_rate"]].copy()
    dup["sec"] = dup["sec"] + rng.integers(5, 600, len(dup))
    dup["redelivery"] = 1
    feed = pd.concat([feed, dup], ignore_index=True).sort_values("sec", kind="stable").reset_index(drop=True)

    t0 = pd.Timestamp(start_dt(rules))
    feed["ts"] = (t0 + pd.to_timedelta(feed["sec"], unit="s")).dt.strftime("%Y-%m-%dT%H:%M:%S")
    feed["event_id"] = [f"E{i + 1:07d}" for i in range(len(feed))]
    cols = ["event_id", "ts", "channel", "channel_order_id", "sku", "qty", "redelivery",
            "sim_ship_after_h", "sim_buyer_cancel_after_min", "sim_return_after_d"]
    return feed[cols]


def main() -> None:
    t = time.time()
    rules = load_rules()
    rng = np.random.default_rng(int(rules["seed"]))
    os.makedirs(DATA, exist_ok=True)

    catalog = build_catalog(rules, rng)
    lam = popularity(rules, rng, len(catalog))
    master = planning_params(catalog, lam, rules, rng)
    listings = build_listings(master, rules, rng)
    feed = build_feed(master, listings, lam, rules, rng)

    master.to_csv(os.path.join(DATA, "sku_master.csv"), index=False)
    listings.to_csv(os.path.join(DATA, "channel_listings.csv"), index=False)
    feed.to_csv(os.path.join(DATA, "orders_feed.csv"), index=False)

    units = master.set_index("sku")["avg_daily_demand"].sort_values(ascending=False)
    top1 = units.head(len(units) // 100).sum() / units.sum()
    print(f"[OK] SKUs: {len(master):,} in {master.category.nunique()} categories, "
          f"{master.supplier_id.nunique()} suppliers")
    print(f"[OK] channel listings: {len(listings):,} rows "
          f"({', '.join(f'{c}={k:,}' for c, k in listings.channel.value_counts().items())})")
    print(f"[OK] order events: {len(feed):,} (re-deliveries {int(feed.redelivery.sum()):,}), "
          f"units {int(feed.qty.sum()):,} over {rules['horizon_days']} days")
    print(f"[OK] top 1% of SKUs = {top1:.0%} of planned demand (Pareto check)")
    print(f"[OK] hot SKUs: {int(master.hot_sku.sum())}, near-stockout at open: "
          f"{int(master.near_stockout_open.sum())}")
    print(f"[OK] done in {time.time() - t:.1f}s")


if __name__ == "__main__":
    main()
