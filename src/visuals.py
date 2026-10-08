"""Render the portfolio slides (PNG) from the pipeline outputs. No browser, pure matplotlib.

    python src/visuals.py      (after pipeline.py)

screens/01-cover.png, 03-race.png, 04-before-after.png, 06-po-alerts.png,
07-reconciliation.png, 08-architecture.png   (02 and 05 are screenshots of the live sheet)
Latin text only - a Cyrillic scan runs at the end.
"""
from __future__ import annotations

import json
import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from common import OUT, SCREENS  # noqa: E402

BG0, CARD, CARD2 = "#0b1220", "#131f33", "#0f1a2e"
ACCENT, RED, AMBER, WHITE, MUTE, GRID = "#34d399", "#f87171", "#f59e0b", "#ffffff", "#9fb0c8", "#22304a"
plt.rcParams["font.family"] = "DejaVu Sans"
MONO = "DejaVu Sans Mono"
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
TEXTS: list[str] = []


def gradient_bg(fig):
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    h, w = 240, 320
    yy = np.linspace(0, 1, h)[:, None]
    xx = np.linspace(0, 1, w)[None, :]
    glow = np.exp(-(((xx - 0.82) ** 2) / 0.05 + ((yy - 0.1) ** 2) / 0.05))
    c0, c1, g = np.array([11, 18, 32]) / 255, np.array([13, 43, 38]) / 255, np.array([52, 211, 153]) / 255
    img = np.zeros((h, w, 3))
    for k in range(3):
        img[:, :, k] = (c0[k] + (c1[k] - c0[k]) * (0 * xx + yy)) * (1 - 0.35 * glow) + g[k] * 0.35 * glow
    ax.imshow(np.clip(img, 0, 1), extent=[0, 1, 0, 1], aspect="auto", origin="upper", zorder=-10)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    return ax


def new_fig(w_in: float = 16, h_in: float = 10):
    fig = plt.figure(figsize=(w_in, h_in), dpi=100)
    return fig, gradient_bg(fig)


def txt(ax, x, y, s, color=WHITE, size=13, bold=False, ha="left", va="center", family=None, **kw):
    TEXTS.append(str(s))
    ax.text(x, y, s, color=color, fontsize=size, fontweight="bold" if bold else "normal",
            ha=ha, va=va, family=family or plt.rcParams["font.family"], **kw)


def card(ax, x, y, w, h, fc=CARD, ec=GRID, lw=1.2):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.012",
                                fc=fc, ec=ec, lw=lw, zorder=1, mutation_aspect=0.6))


def kicker(ax, s, y=0.93):
    txt(ax, 0.06, y, s, color=ACCENT, size=15, bold=True, family=MONO)


def heading(ax, s, y=0.86, size=27):
    txt(ax, 0.06, y, s, size=size, bold=True, va="top")


def save(fig, name):
    fig.savefig(os.path.join(SCREENS, name), facecolor=BG0)
    plt.close(fig)


def load() -> dict:
    with open(os.path.join(OUT, "metrics.json"), encoding="utf-8") as f:
        m = json.load(f)
    ba = pd.read_excel(os.path.join(OUT, "07_Before_vs_After.xlsx"), sheet_name=None)
    return {"m": m, "hl": m["headline"], "comp": ba["Comparison"], "daily": ba["Daily"],
            "po": pd.read_excel(os.path.join(OUT, "03_Purchase_Order_Drafts.xlsx"), sheet_name="PO lines"),
            "alerts": pd.read_excel(os.path.join(OUT, "04_Low_Stock_Alerts.xlsx")),
            "recon": pd.read_excel(os.path.join(OUT, "05_Reconciliation.xlsx"), sheet_name=None),
            "master": pd.read_excel(os.path.join(OUT, "01_Inventory_Master.xlsx"))}


SHORT = {"once a day (by hand)": "Once a day\n(by hand)", "every 4 hours": "Every\n4 hours",
         "every hour": "Every\nhour", "every 15 minutes": "Every\n15 min",
         "event-time reservation + push": "This\nengine"}


def bars(ax_, comp: pd.DataFrame, label_size=11):
    labels = [SHORT.get(s, s) for s in comp["stock sync"]]
    vals = comp["oversold orders"].to_numpy()
    colors = [RED] * (len(vals) - 1) + [ACCENT]
    ax_.bar(range(len(vals)), vals, color=colors, width=0.62)
    for i, v in enumerate(vals):
        ax_.text(i, v + max(vals) * 0.03, f"{v:,}", color=WHITE, ha="center", fontsize=label_size + 2,
                 fontweight="bold")
    ax_.set_xticks(range(len(vals)))
    ax_.set_xticklabels(labels, color=MUTE, fontsize=label_size)
    ax_.set_yticks([])
    ax_.set_ylim(0, max(vals) * 1.22)
    ax_.set_facecolor("none")
    for s in ax_.spines.values():
        s.set_visible(False)
    TEXTS.extend(labels)


# ------------------------------------------------------------------ slides
def s01_cover(d):
    hl = d["hl"]
    fig, ax = new_fig(16, 12)
    txt(ax, 0.07, 0.915, "GOOGLE SHEETS  .  PYTHON  .  REST API  .  INVENTORY OPS", color=ACCENT,
        size=16, bold=True, family=MONO)
    txt(ax, 0.07, 0.83, "10,000 SKUs. 3 Sales Channels.", size=50, bold=True, va="top")
    txt(ax, 0.07, 0.735, "Zero Oversells.", color=ACCENT, size=50, bold=True, va="top")
    txt(ax, 0.07, 0.645, "Stock is reserved the moment an order lands and pushed to every channel in seconds;",
        color=MUTE, size=18, va="top")
    txt(ax, 0.07, 0.61, "reorders, low-stock alerts and nightly reconciliation run on their own.",
        color=MUTE, size=18, va="top")
    chips = [("10,000", "SKUs in sync"),
             (f"{hl['oversold_before_daily_sync']} → 0", "oversold orders, 60 days"),
             (f"{hl['order_events'] / 1000:,.0f}k", "order events processed"),
             (f"{hl['pos_drafted']:,}", "supplier PO drafts")]
    cw, gap, x0, y0, chh = 0.205, 0.015, 0.07, 0.40, 0.14
    for i, (big, lab) in enumerate(chips):
        x = x0 + i * (cw + gap)
        card(ax, x, y0, cw, chh)
        txt(ax, x + cw / 2, y0 + chh * 0.6, big, color=ACCENT, size=28, bold=True, ha="center")
        txt(ax, x + cw / 2, y0 + chh * 0.22, lab, color=MUTE, size=13.5, ha="center")
    card(ax, 0.07, 0.06, 0.86, 0.29, fc=CARD2)
    txt(ax, 0.09, 0.325, "OVERSOLD ORDERS IN 60 DAYS, BY HOW STOCK IS SYNCED", size=14, bold=True, va="top")
    bars(fig.add_axes([0.11, 0.115, 0.78, 0.165]), d["comp"], label_size=10)
    txt(ax, 0.93, 0.035, "Demo on a synthetic dataset", color="#5b6b84", size=11, ha="right")
    save(fig, "01-cover.png")


def lane(ax, y, title, events, color):
    txt(ax, 0.06, y + 0.105, title, size=17, bold=True)
    ax.plot([0.08, 0.92], [y, y], color=GRID, lw=3, zorder=2)
    for x, label, sub, col in events:
        ax.scatter([x], [y], s=170, color=col, zorder=3)
        txt(ax, x, y + 0.045, label, color=col, size=12.5, bold=True, ha="center")
        for k, line in enumerate(sub.split("\n")):
            txt(ax, x, y - 0.045 - k * 0.032, line, color=MUTE, size=11.5, ha="center")


def s03_race(d):
    hl = d["hl"]
    fig, ax = new_fig()
    kicker(ax, "HOW AN OVERSELL HAPPENS  (and how the engine closes the gap)")
    heading(ax, "3 units left. Two buyers on two channels, one minute apart.", size=25)
    card(ax, 0.04, 0.47, 0.92, 0.29, fc="#1a1322", ec="#4a2a3a")
    lane(ax, 0.58, "Scheduled sync (every 15 minutes)", [
        (0.14, "10:00:00", "Online Store sells 2\nstock 3 -> 1", WHITE),
        (0.38, "10:00 - 10:15", "Marketplace A still shows 3\n(waits for the next sync)", AMBER),
        (0.62, "10:01:00", "buyer orders 2\n-> accepted", AMBER),
        (0.86, "OVERSOLD", "4 sold, 3 in stock\nseller cancels, penalty", RED)], RED)
    card(ax, 0.04, 0.12, 0.92, 0.29, fc="#13261f", ec=ACCENT, lw=1.6)
    lane(ax, 0.23, "This engine (reserve at intake, push in seconds)", [
        (0.14, "10:00:00", "Online Store sells 2\n2 reserved in ledger", WHITE),
        (0.38, "10:00:12", "push: Marketplace A\nqty 3 -> 1", ACCENT),
        (0.62, "10:01:00", "buyer sees 1 unit\ncannot order 2", ACCENT),
        (0.86, "NO OVERSELL", "listings match\nreal stock", ACCENT)], ACCENT)
    txt(ax, 0.5, 0.055, f"Measured on the same 60-day order stream: {hl['oversold_before_daily_sync']} oversold "
        f"orders with a daily stock update, {hl['oversold_before_15min_sync']} with a 15-minute sync, "
        f"{hl['oversold_after']} with the engine.", color=WHITE, size=13, ha="center", bold=True)
    save(fig, "03-race.png")


def s04_before_after(d):
    hl, daily = d["hl"], d["daily"]
    fig, ax = new_fig()
    kicker(ax, "BEFORE vs AFTER  (one order stream, 156,057 events, 60 days)")
    heading(ax, f"Oversold orders: {hl['oversold_before_daily_sync']} → 0, while selling more", size=27)
    card(ax, 0.04, 0.30, 0.44, 0.47)
    txt(ax, 0.06, 0.745, "Oversold orders by sync strategy", size=14, bold=True)
    bars(fig.add_axes([0.06, 0.37, 0.40, 0.33]), d["comp"])
    card(ax, 0.52, 0.30, 0.44, 0.47)
    txt(ax, 0.54, 0.745, "Oversold orders per day", size=14, bold=True)
    lax = fig.add_axes([0.56, 0.36, 0.38, 0.34])
    lax.set_facecolor("none")
    x = np.arange(len(daily))
    cols = list(daily.columns)
    lax.plot(x, daily[cols[1]], color=RED, lw=2.2, label="stock updated once a day")
    lax.plot(x, daily[cols[4]], color=AMBER, lw=2.2, label="sync every 15 min")
    lax.plot(x, daily[cols[5]], color=ACCENT, lw=3.5, label="this engine")
    lax.tick_params(colors=MUTE, labelsize=10)
    lax.set_xlabel("day of the 60-day run", color=MUTE)
    for s in lax.spines.values():
        s.set_color(GRID)
    lax.legend(facecolor=CARD, edgecolor=GRID, labelcolor=WHITE, fontsize=10, loc="upper right")
    TEXTS.extend(["stock updated once a day", "sync every 15 min", "this engine", "day of the 60-day run"])
    stats = [(f"+{hl['extra_units_sold_vs_daily']:,}", "more units sold vs daily update"),
             (f"{hl['phantom_units_before']:,}", "units no longer locked by duplicate webhooks"),
             (f"${hl['cancel_cost_avoided_low'] / 1000:,.1f}k-{hl['cancel_cost_avoided_high'] / 1000:,.1f}k",
              "cancellation cost avoided (60 days)")]
    for i, (big, lab) in enumerate(stats):
        x0 = 0.04 + i * 0.31
        card(ax, x0, 0.08, 0.29, 0.17)
        txt(ax, x0 + 0.145, 0.185, big, color=ACCENT, size=26, bold=True, ha="center")
        txt(ax, x0 + 0.145, 0.115, lab, color=MUTE, size=12, ha="center")
    txt(ax, 0.96, 0.035, r"Cost per cancelled order: \$41.77-\$105.63 industry benchmark (nventory.io). "
        "Synthetic demo data.", color="#5b6b84", size=10, ha="right")
    save(fig, "04-before-after.png")


def pick_po(po: pd.DataFrame) -> pd.DataFrame:
    """A 5-6 line PO with the largest order volume - the most representative example."""
    g = po.groupby("po_id").agg(lines=("sku", "size"), units=("suggested_qty", "sum"))
    pid = g[(g.lines >= 5) & (g.lines <= 6)].sort_values("units").index[-1]
    return po[po.po_id == pid]


def pick_digest(alerts: pd.DataFrame) -> pd.DataFrame:
    alerts = alerts.copy()
    alerts["ts"] = pd.to_datetime(alerts["ts"])
    later = alerts[alerts["ts"] >= alerts["ts"].min() + pd.Timedelta(days=20)]
    sizes = later.groupby("ts").size()
    t = sizes[(sizes >= 5) & (sizes <= 7)].index[0]
    return later[later["ts"] == t]


def s06_po_alerts(d):
    hl = d["hl"]
    po, dig = pick_po(d["po"]), pick_digest(d["alerts"])
    names = d["master"].set_index("sku")["name"]
    fig, ax = new_fig()
    kicker(ax, "REPLENISHMENT ON AUTOPILOT")
    heading(ax, "Nightly supplier POs and one low-stock digest per cycle", size=26)
    card(ax, 0.04, 0.12, 0.47, 0.66)
    head = po.iloc[0]
    txt(ax, 0.06, 0.745, f"{head['po_id']}  |  {head['supplier_id']}  |  DRAFT", size=15, bold=True)
    txt(ax, 0.06, 0.705, f"drafted {pd.Timestamp(head['drafted']):%b %d, %H:%M}   ETA "
        f"{pd.Timestamp(head['eta']):%b %d}", color=MUTE, size=12)
    txt(ax, 0.06, 0.645, f"{'SKU':11}{'position':>9}{'reorder pt':>11}{'EOQ':>7}", size=12.5,
        bold=True, family=MONO)
    txt(ax, 0.49, 0.645, "order qty", size=12.5, bold=True, family=MONO, ha="right")
    y = 0.595
    for _, r in po.iterrows():
        txt(ax, 0.06, y, f"{r['sku']:11}{r['stock_position']:>9,}{r['reorder_point']:>11,}{r['eoq']:>7,}",
            color=MUTE, size=12.5, family=MONO)
        txt(ax, 0.49, y, f"{r['suggested_qty']:,}", color=ACCENT, size=12.5, bold=True, family=MONO, ha="right")
        y -= 0.055
    txt(ax, 0.06, 0.17, "Lines are grouped per supplier. Qty = EOQ, or enough to clear the reorder point.",
        color=MUTE, size=11)
    card(ax, 0.53, 0.12, 0.43, 0.66, fc="#f8fafc", ec="#cbd5e1")
    txt(ax, 0.55, 0.745, f"Low stock: {len(dig)} SKUs crossed their reorder point", color="#0f172a",
        size=14, bold=True)
    txt(ax, 0.55, 0.705, f"Inventory Sync  |  {pd.Timestamp(dig['ts'].iloc[0]):%b %d, %H:%M}",
        color="#64748b", size=11.5)
    txt(ax, 0.55, 0.645, f"{'SKU':11}{'available':>10}{'reorder pt':>12}{'on order':>10}", color="#0f172a",
        size=12, bold=True, family=MONO)
    y = 0.595
    for _, r in dig.iterrows():
        txt(ax, 0.55, y, f"{r['sku']:11}{r['available']:>10,}{r['reorder_point']:>12,}{r['on_order']:>10,}",
            color="#334155", size=12, family=MONO)
        txt(ax, 0.55, y - 0.025, str(names.get(r["sku"], ""))[:38], color="#94a3b8", size=9.5)
        y -= 0.065
    txt(ax, 0.55, 0.17, "Edge-triggered: one alert per crossing, then quiet for 24h.", color="#64748b", size=11)
    txt(ax, 0.5, 0.06, f"60 days: {hl['pos_drafted']:,} PO drafts ({hl['po_lines']:,} lines)  |  "
        f"{hl['alerts']:,} alerts in {hl['alert_digests']:,} digests instead of "
        f"{hl['level_alerts'] / 1e6:,.1f}M repeated 'still low' pings", size=13, bold=True, ha="center")
    save(fig, "06-po-alerts.png")


def s07_recon(d):
    hl = d["hl"]
    checks = d["recon"]["Invariant checks"]
    drifts = d["recon"]["Drifts healed"]
    fig, ax = new_fig()
    kicker(ax, "TRUST, BUT VERIFY")
    heading(ax, "Every run proves itself: 10 invariant checks + nightly reconciliation", size=25)
    card(ax, 0.04, 0.12, 0.50, 0.66)
    txt(ax, 0.06, 0.745, "Invariant checks (run fails loudly if one breaks)", size=14, bold=True)
    y = 0.685
    for _, r in checks.iterrows():
        ok = r["result"] == "PASS"
        txt(ax, 0.06, y, "PASS" if ok else "FAIL", color=ACCENT if ok else RED, size=11.5, bold=True, family=MONO)
        txt(ax, 0.115, y, r["check"], color=WHITE, size=12.5)
        y -= 0.055
    card(ax, 0.56, 0.12, 0.40, 0.66)
    txt(ax, 0.58, 0.745, "Nightly reconciliation", size=14, bold=True)
    txt(ax, 0.58, 0.685, f"{hl['drifts_healed']}", color=ACCENT, size=34, bold=True)
    txt(ax, 0.67, 0.69, "channel updates that 'succeeded'\nbut never landed - found and healed",
        color=MUTE, size=12)
    txt(ax, 0.58, 0.595, f"{hl['hidden_units_restored']:,}", color=ACCENT, size=34, bold=True)
    txt(ax, 0.70, 0.60, "units put back on sale", color=MUTE, size=12)
    txt(ax, 0.58, 0.515, f"{'channel':9}{'sku':11}{'showed':>7}{'should':>7}", size=11.5, bold=True, family=MONO)
    y = 0.47
    for _, r in drifts.head(6).iterrows():
        txt(ax, 0.58, y, f"{r['channel']:9}{r['sku']:11}{r['channel_showed']:>7,}{r['hub_expected']:>7,}",
            color=MUTE, size=11.5, family=MONO)
        y -= 0.045
    txt(ax, 0.5, 0.06, "Replay test: the full 156,057-event feed delivered twice -> 0 extra reservations, "
        "identical results.", size=13, bold=True, ha="center")
    save(fig, "07-reconciliation.png")


def box(ax, x, y, w, h, title, lines, ec=GRID, fc=CARD, tc=WHITE):
    card(ax, x, y, w, h, fc=fc, ec=ec, lw=1.6)
    txt(ax, x + 0.015, y + h - 0.035, title, color=tc, size=13.5, bold=True)
    for k, line in enumerate(lines):
        txt(ax, x + 0.015, y + h - 0.08 - k * 0.038, line, color=MUTE, size=11.5)


def arrow(ax, p0, p1, color=ACCENT, both=False):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="<|-|>" if both else "-|>", mutation_scale=18,
                                 color=color, lw=2, zorder=4))


def s08_architecture(d):
    fig, ax = new_fig()
    kicker(ax, "ARCHITECTURE")
    heading(ax, "One source of truth, event-time sync, nightly autopilot", size=26)
    for k, name in enumerate(["Online Store", "Marketplace A", "Marketplace B"]):
        box(ax, 0.04, 0.60 - k * 0.165, 0.19, 0.13, name, ["orders -> webhook", "<- qty updates"])
    box(ax, 0.32, 0.30, 0.34, 0.43, "Sync engine (Python)", [
        "1. idempotency check (channel order id)",
        "2. reserve in append-only ledger",
        "3. available = on hand - reserved - buffer",
        "4. push qty to every channel in seconds",
        "5. read-back on decreases, retry",
        "6. audit log of every change"], ec=ACCENT, fc="#13261f")
    box(ax, 0.75, 0.55, 0.21, 0.18, "Google Sheets", ["inventory master (10k SKUs)", "live dashboard + KPIs",
                                                      "PO drafts, alerts, audit"])
    box(ax, 0.75, 0.30, 0.21, 0.19, "Nightly job", ["supplier PO drafts (ROP/EOQ)", "ledger audit",
                                                    "channel reconciliation"])
    box(ax, 0.32, 0.10, 0.34, 0.14, "Alerts", ["low-stock digest, edge-triggered,", "24h cooldown per SKU"])
    for k in range(3):
        yc = 0.665 - k * 0.165
        arrow(ax, (0.23, yc), (0.32, 0.52 + (1 - k) * 0.06), both=True)
    arrow(ax, (0.66, 0.6), (0.75, 0.63), both=True)
    arrow(ax, (0.66, 0.4), (0.75, 0.4))
    arrow(ax, (0.49, 0.30), (0.49, 0.24))
    for k, f in enumerate(["available = on_hand - reserved - buffer",
                           "reorder point = d x L + Z x sigma x sqrt(L)",
                           "EOQ = sqrt(2 x D x S / H)"]):
        txt(ax, 0.04 + k * 0.32, 0.045, f, color=ACCENT, size=12, family=MONO)
    save(fig, "08-architecture.png")


def sheet_slide(src: str, name: str, kick: str, head: str) -> None:
    """Frame a Google-rendered snapshot of the live sheet (see sheet_snapshots.py)."""
    path = os.path.join(SCREENS, src)
    if not os.path.exists(path):
        print(f"[..] skip {name}: run src/sheet_snapshots.py first")
        return
    img = plt.imread(path)
    fig, ax = new_fig()
    kicker(ax, kick)
    heading(ax, head, size=25)
    box_w, box_h, top = 0.92, 0.72, 0.80
    aspect = (img.shape[1] / img.shape[0]) * (10 / 16)
    w = min(box_w, box_h * aspect)
    h = w / aspect
    x0 = (1 - w) / 2
    card(ax, x0 - 0.008, top - h - 0.012, w + 0.016, h + 0.024, fc="#ffffff", ec=GRID)
    fig.add_axes([x0, top - h, w, h]).imshow(img)
    fig.axes[-1].set_axis_off()
    save(fig, name)


def main() -> None:
    os.makedirs(SCREENS, exist_ok=True)
    d = load()
    for fn in (s01_cover, s03_race, s04_before_after, s06_po_alerts, s07_recon, s08_architecture):
        fn(d)
    sheet_slide("sheet-dashboard.png", "02-live-dashboard.png", "LIVE GOOGLE SHEET  -  DASHBOARD",
                "Every tile is a formula over the 10,000-row inventory tab")
    sheet_slide("sheet-inventory.png", "05-live-inventory.png", "LIVE GOOGLE SHEET  -  INVENTORY",
                "Available, status and per-channel qty for every SKU")
    pngs = sorted(f for f in os.listdir(SCREENS) if f.endswith(".png"))
    print("[OK] rendered:", ", ".join(pngs))
    hits = sum(1 for s in TEXTS if CYRILLIC.search(s))
    print(f"[{'OK' if hits == 0 else 'WARN'}] cyrillic strings on slides: {hits}")


if __name__ == "__main__":
    main()
