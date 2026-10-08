"""Publish the engine run into a live Google Sheet (the portfolio demo).

    python src/to_gsheet.py [spreadsheet id or url]

Tabs: Dashboard | Inventory | Before vs After | Daily | PO Drafts | Low-Stock Alerts |
      Reservations | Reconciliation | Audit Log | Run Summary | READ ME
Inventory status and every Dashboard tile are live formulas over the Inventory tab, so
editing a stock number re-computes availability, status and KPIs in the sheet itself.
ASCII-only console output.
"""
from __future__ import annotations

import json
import os
import sys

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

from common import OUT

SHEET_ID = "1xuyR0t6gkZ9dnGQ21nkTpJg70_qXwlOIxhREZTqfhgM"
REPO = "https://github.com/Zhigerisen/multi-channel-inventory-sync"
# service-account key: env GOOGLE_CREDENTIALS, else ~/.claude/google-credentials.json
CREDS = os.environ.get("GOOGLE_CREDENTIALS", os.path.expanduser("~/.claude/google-credentials.json"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
CHUNK = 2500
# SKU, Product, Category, Supplier, 6 stock columns, Status, 3 channel columns, cost, price, value
INVENTORY_WIDTHS = [84, 250, 140, 74] + [90] * 6 + [90] + [112] * 3 + [80, 80, 96]
SAMPLE = 3000
TABS = ["Dashboard", "Inventory", "Before vs After", "Daily", "PO Drafts", "Low-Stock Alerts",
        "Reservations", "Reconciliation", "Audit Log", "Run Summary", "READ ME"]
DARK, ACCENT, MUTED = {"red": .043, "green": .071, "blue": .125}, {"red": .204, "green": .827, "blue": .6}, \
    {"red": .45, "green": .5, "blue": .58}
WHITE = {"red": 1, "green": 1, "blue": 1}
# SKUs furthest below their reorder point first; zero-demand SKUs (reorder point 0) are not alerts
NEEDS_ATTENTION = ('=ARRAY_CONSTRAIN(SORT(FILTER({Inventory!A2:A,Inventory!G2:G,Inventory!I2:I,'
                   'Inventory!H2:H,Inventory!K2:K,Inventory!G2:G-Inventory!I2:I},'
                   'Inventory!K2:K<>"ACTIVE",Inventory!I2:I>0),6,TRUE),14,5)')


def rgb(hex_: str) -> dict:
    h = hex_.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def values(df: pd.DataFrame) -> list[list]:
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M")
    df = df.astype(object).where(df.notna(), "")
    return [list(df.columns)] + df.values.tolist()


def tab(sh, title: str, rows: int, cols: int):
    """Fresh worksheet on every run (old formats, rules and charts must not pile up)."""
    try:
        old = sh.worksheet(title)
        if len(sh.worksheets()) == 1:
            old.clear()
            old.resize(rows=max(rows, 2), cols=max(cols, 2))
            return old
        sh.del_worksheet(old)
    except gspread.WorksheetNotFound:
        pass
    return sh.add_worksheet(title=title, rows=max(rows, 2), cols=max(cols, 2))


def put(sh, title: str, data: list[list], header_fmt: bool = True):
    ws = tab(sh, title, len(data) + 1, max(len(r) for r in data))
    for i in range(0, len(data), CHUNK):
        ws.update(range_name=f"A{i + 1}", values=data[i:i + CHUNK], value_input_option="USER_ENTERED")
    if header_fmt:
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True, "foregroundColor": WHITE},
                          "backgroundColor": DARK})
    print(f"[OK] tab {title}: {len(data) - 1:,} rows")
    return ws


def inventory_rows(df: pd.DataFrame) -> list[list]:
    head = ["SKU", "Product", "Category", "Supplier", "On hand", "Reserved", "Available", "On order",
            "Reorder pt", "EOQ", "Status", "Online Store", "Marketplace A", "Marketplace B",
            "Unit cost", "Price", "Stock value"]
    rows = [head]
    for i, r in enumerate(df.itertuples(index=False), start=2):
        off = [("" if v < 0 else int(v)) for v in (r.offered_WEB, r.offered_MKA, r.offered_MKB)]
        rows.append([r.sku, r.name, r.category, r.supplier_id, int(r.on_hand), int(r.reserved),
                     f"=E{i}-F{i}", int(r.on_order), int(r.reorder_point), int(r.eoq),
                     f'=IF(G{i}<=0,"OUT",IF(G{i}+H{i}<=I{i},"REORDER",IF(G{i}<=I{i},"LOW","ACTIVE")))',
                     *off, float(r.cost), float(r.price), f"=E{i}*O{i}"])
    return rows


def dashboard(sh, hl: dict) -> None:
    ws = tab(sh, "Dashboard", 60, 12)
    v = lambda key: f'=VLOOKUP("{key}",\'Run Summary\'!A:B,2,FALSE)'  # noqa: E731
    cells = [
        ["Multi-Channel Inventory Sync  -  Live Dashboard"],
        ["10,000 SKUs  |  3 sales channels  |  60-day order stream  |  demo on a synthetic dataset"],
        [],
        ["Total SKUs", "", "Available units", "", "Inventory value", "", "Low / reorder", "",
         "Out of stock", "", "PO drafts (60 days)", ""],
        ["=COUNTA(Inventory!A2:A)", "", "=SUM(Inventory!G2:G)", "", "=SUM(Inventory!Q2:Q)", "",
         '=COUNTIF(Inventory!K2:K,"LOW")+COUNTIF(Inventory!K2:K,"REORDER")', "",
         '=COUNTIF(Inventory!K2:K,"OUT")', "", v("pos_drafted"), ""],
        ["Oversold orders: daily sync", "", "Oversold orders: engine", "", "Duplicate webhooks ignored", "",
         "Channel drifts healed", "", "Low-stock alerts", "", "Order events processed", ""],
        [v("oversold_before_daily_sync"), "", v("oversold_after"), "", v("duplicate_webhooks_ignored"), "",
         v("drifts_healed"), "", v("alerts"), "", v("order_events"), ""],
        [],
        ["Sales channels", "", "", "", "", "", "Needs attention: lowest stock vs reorder point"],
        ["Channel", "Listings", "Units offered", "At 0 qty", "", "",
         "SKU", "Available", "Reorder point", "On order", "Status"],
        ["Online Store", "=COUNT(Inventory!L2:L)", "=SUM(Inventory!L2:L)", "=COUNTIF(Inventory!L2:L,0)", "", "",
         NEEDS_ATTENTION],
        ["Marketplace A", "=COUNT(Inventory!M2:M)", "=SUM(Inventory!M2:M)", "=COUNTIF(Inventory!M2:M,0)"],
        ["Marketplace B", "=COUNT(Inventory!N2:N)", "=SUM(Inventory!N2:N)", "=COUNTIF(Inventory!N2:N,0)"],
        [],
        ["Oversold orders by sync strategy", "", "", "", ""],
        ["Stock sync", "Oversold orders", "Units sold", "", ""],
        *[[f"='Before vs After'!B{k}", f"='Before vs After'!C{k}", f"='Before vs After'!E{k}"] for k in range(2, 8)],
    ]
    ws.update(range_name="A1", values=cells, value_input_option="USER_ENTERED")
    sid = ws.id
    reqs = [
        {"mergeCells": {"range": grid(sid, 0, 1, 0, 12), "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": grid(sid, 1, 2, 0, 12), "mergeType": "MERGE_ALL"}},
        fmt(sid, 0, 1, 0, 12, {"backgroundColor": DARK, "textFormat": {"bold": True, "fontSize": 18,
                                                                      "foregroundColor": WHITE}}),
        fmt(sid, 1, 2, 0, 12, {"backgroundColor": DARK, "textFormat": {"fontSize": 10, "foregroundColor": ACCENT}}),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                                 "startIndex": 0, "endIndex": 12},
                                       "properties": {"pixelSize": 118}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS",
                                                 "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
    ]
    for row in (3, 5):                       # KPI tiles: label row + value row, 2 columns each
        for c in range(0, 12, 2):
            reqs += [{"mergeCells": {"range": grid(sid, row, row + 1, c, c + 2), "mergeType": "MERGE_ALL"}},
                     {"mergeCells": {"range": grid(sid, row + 1, row + 2, c, c + 2), "mergeType": "MERGE_ALL"}}]
        reqs += [fmt(sid, row, row + 1, 0, 12, {"backgroundColor": rgb("#131f33"),
                                                "textFormat": {"fontSize": 9, "foregroundColor": rgb("#9fb0c8")},
                                                "horizontalAlignment": "CENTER"}),
                 fmt(sid, row + 1, row + 2, 0, 12, {"backgroundColor": rgb("#131f33"),
                                                    "textFormat": {"bold": True, "fontSize": 20,
                                                                   "foregroundColor": ACCENT},
                                                    "horizontalAlignment": "CENTER",
                                                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})]
    reqs += [fmt(sid, 4, 5, 4, 6, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}}),
             fmt(sid, 6, 7, 0, 2, {"textFormat": {"bold": True, "fontSize": 20, "foregroundColor": rgb("#f87171")}}),
             fmt(sid, 8, 9, 0, 12, {"textFormat": {"bold": True, "fontSize": 11}}),
             fmt(sid, 14, 15, 0, 5, {"textFormat": {"bold": True, "fontSize": 11}}),
             fmt(sid, 9, 10, 0, 4, {"backgroundColor": DARK, "textFormat": {"bold": True, "foregroundColor": WHITE}}),
             fmt(sid, 9, 10, 6, 11, {"backgroundColor": DARK, "textFormat": {"bold": True, "foregroundColor": WHITE}}),
             fmt(sid, 15, 16, 0, 3, {"backgroundColor": DARK, "textFormat": {"bold": True, "foregroundColor": WHITE}}),
             fmt(sid, 10, 13, 1, 4, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}),
             fmt(sid, 16, 22, 1, 3, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}),
             fmt(sid, 21, 22, 0, 3, {"textFormat": {"italic": True, "foregroundColor": rgb("#64748b")}}),
             fmt(sid, 20, 21, 0, 3, {"backgroundColor": rgb("#e8faf2"), "textFormat": {"bold": True}}),
             {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True}},
                                        "fields": "gridProperties.hideGridlines"}}]
    sh.batch_update({"requests": reqs})
    print("[OK] tab Dashboard: tiles + tables")


def grid(sid: int, r0: int, r1: int, c0: int, c1: int) -> dict:
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}


def fmt(sid: int, r0: int, r1: int, c0: int, c1: int, f: dict) -> dict:
    return {"repeatCell": {"range": grid(sid, r0, r1, c0, c1), "cell": {"userEnteredFormat": f},
                           "fields": "userEnteredFormat(" + ",".join(f) + ")"}}


def inventory_formats(sh) -> None:
    ws = sh.worksheet("Inventory")
    sid, n = ws.id, ws.row_count
    rule = lambda text, bg, fg: {"addConditionalFormatRule": {"index": 0, "rule": {  # noqa: E731
        "ranges": [grid(sid, 1, n, 10, 11)],
        "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": text}]},
                        "format": {"backgroundColor": rgb(bg), "textFormat": {"bold": True, "foregroundColor": rgb(fg)}}}}}}
    sh.batch_update({"requests": [
        fmt(sid, 1, n, 4, 10, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}),
        fmt(sid, 1, n, 11, 14, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}),
        fmt(sid, 1, n, 14, 16, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}),
        fmt(sid, 1, n, 16, 17, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}}),
        rule("OUT", "#fde2e1", "#b91c1c"), rule("REORDER", "#ffedd5", "#c2410c"),
        rule("LOW", "#fef3c7", "#a16207"), rule("ACTIVE", "#ecfdf5", "#047857"),
        *[{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i,
                                                   "endIndex": i + 1}, "properties": {"pixelSize": px},
                                         "fields": "pixelSize"}} for i, px in enumerate(INVENTORY_WIDTHS)],
        {"setBasicFilter": {"filter": {"range": grid(sid, 0, n, 0, 17)}}},
    ]})
    print("[OK] Inventory formats + status colours + filter")


def charts(sh) -> None:
    dash, daily, comp = sh.worksheet("Dashboard"), sh.worksheet("Daily"), sh.worksheet("Before vs After")
    def src(ws, c, r1):
        return {"sourceRange": {"sources": [grid(ws.id, 0, r1, c, c + 1)]}}
    line = {"addChart": {"chart": {"spec": {
        "title": "Oversold orders per day",
        "basicChart": {"chartType": "LINE", "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
                       "axis": [{"position": "LEFT_AXIS", "title": "oversold orders"}],
                       "domains": [{"domain": src(daily, 0, 61)}],
                       "series": [{"series": src(daily, 1, 61), "targetAxis": "LEFT_AXIS",
                                   "color": rgb("#f87171")},
                                  {"series": src(daily, 4, 61), "targetAxis": "LEFT_AXIS",
                                   "color": rgb("#f59e0b")},
                                  {"series": src(daily, 5, 61), "targetAxis": "LEFT_AXIS",
                                   "color": rgb("#10b981"), "lineStyle": {"width": 4}}]}},
        "position": {"overlayPosition": {"anchorCell": {"sheetId": dash.id, "rowIndex": 25, "columnIndex": 0},
                                         "widthPixels": 700, "heightPixels": 330}}}}}
    bar = {"addChart": {"chart": {"spec": {
        "title": "Oversold orders in 60 days, by sync strategy",
        "basicChart": {"chartType": "COLUMN", "legendPosition": "NO_LEGEND", "headerCount": 1,
                       "domains": [{"domain": src(comp, 1, 6)}],
                       "series": [{"series": src(comp, 2, 6), "targetAxis": "LEFT_AXIS",
                                   "color": rgb("#f87171"),
                                   "dataLabel": {"type": "DATA", "placement": "OUTSIDE_END"}}]}},
        "position": {"overlayPosition": {"anchorCell": {"sheetId": dash.id, "rowIndex": 25, "columnIndex": 6},
                                         "offsetXPixels": 20, "widthPixels": 690, "heightPixels": 330}}}}}
    sh.batch_update({"requests": [line, bar]})
    print("[OK] charts on Dashboard")


def run_summary(hl: dict, engine: dict) -> list[list]:
    keep = {k: v for k, v in hl.items() if not isinstance(v, dict)}
    keep.update({f"engine_{k}": v for k, v in engine.items()})
    return [["metric", "value"]] + [[k, v] for k, v in keep.items()]


def readme(hl: dict) -> list[list]:
    return [
        ["Multi-Channel Inventory Sync - demo workbook"],
        [],
        ["What this is", "A 10,000-SKU catalogue sold on 3 channels. One 60-day order stream "
                         f"({hl['order_events']:,} events) was run through two ways of syncing stock:"],
        ["", "1) a scheduled job pushing the same qty to every channel (once a day ... every 15 min)"],
        ["", "2) this engine: push each stock change to every channel within seconds."],
        ["", "Both share reservation at intake, webhook de-duplication and nightly PO drafts."],
        ["Result", f"Oversold orders: {hl['oversold_before_daily_sync']} with a daily sync, "
                   f"{hl['oversold_before_15min_sync']} with a 15-minute sync, "
                   f"{hl['oversold_after']} with the engine (push latency {hl['push_latency_sec']} s), "
                   f"{hl['oversold_after_stress']} if pushes were {hl['stress_latency_multiplier']}x slower."],
        ["Live formulas", "Inventory > Available and Status and the stock KPI tiles are formulas over the "
                          "Inventory tab; run metrics (oversells, alerts...) come from the engine run."],
        ["Data", "100% SYNTHETIC (fixed random seed). No real brands, stores, customers or orders. "
                 "Channel APIs are simulated; this sheet is the published result of the run."],
        ["Source code", REPO],
        ["Samples", f"Reservations / Audit Log tabs show the latest {SAMPLE:,} rows; the full ledger "
                    "and audit log are CSV files in the repository."],
    ]


def main(target: str) -> None:
    gc =gspread.authorize(Credentials.from_service_account_file(CREDS, scopes=SCOPES))
    sh = gc.open_by_url(target) if target.startswith("http") else gc.open_by_key(target)
    print("[..] opened:", sh.title)
    sh.batch_update({"requests": [{"updateSpreadsheetProperties": {
        "properties": {"locale": "en_US", "timeZone": "Etc/GMT"}, "fields": "locale,timeZone"}}]})

    with open(os.path.join(OUT, "metrics.json"), encoding="utf-8") as f:
        metrics = json.load(f)
    hl = metrics["headline"]
    state = pd.read_excel(os.path.join(OUT, "01_Inventory_Master.xlsx"))
    ba = pd.read_excel(os.path.join(OUT, "07_Before_vs_After.xlsx"), sheet_name=None)
    po = pd.read_excel(os.path.join(OUT, "03_Purchase_Order_Drafts.xlsx"), sheet_name="PO lines")
    alerts = pd.read_excel(os.path.join(OUT, "04_Low_Stock_Alerts.xlsx"))
    recon = pd.read_excel(os.path.join(OUT, "05_Reconciliation.xlsx"), sheet_name=None)
    ledger = pd.read_csv(os.path.join(OUT, "02_Reservations_Ledger.csv.gz")).tail(SAMPLE)
    audit = pd.read_csv(os.path.join(OUT, "06_Audit_Log.csv.gz")).tail(SAMPLE)

    first = sh.get_worksheet(0)
    if first.title not in TABS:
        first.update_title("Dashboard")
    put(sh, "Run Summary", run_summary(hl, metrics["engine"]))
    put(sh, "Inventory", inventory_rows(state))
    inventory_formats(sh)
    comp = ba["Comparison"].copy()
    comp["stock sync"] = comp["stock sync"].replace({
        "once a day (by hand)": "daily, by hand", "every 15 minutes": "every 15 min",
        "event-time reservation + push": "this engine"}).str.replace(
        r"^pushes (\d+)x slower$", r"stress: \1x slower", regex=True)
    put(sh, "Before vs After", values(comp))
    daily_ws = put(sh, "Daily", values(ba["Daily"]))
    sh.batch_update({"requests": [fmt(daily_ws.id, 1, daily_ws.row_count, 0, 1,
                                      {"numberFormat": {"type": "DATE", "pattern": "mmm d"}})]})
    put(sh, "PO Drafts", values(po))
    put(sh, "Low-Stock Alerts", values(alerts))
    put(sh, "Reservations", values(ledger))
    drifts = recon["Drifts healed"]
    checks = recon["Invariant checks"]
    rc = values(drifts) + [[], ["Invariant checks (every run)", "result"]] + values(checks)[1:]
    put(sh, "Reconciliation", rc)
    put(sh, "Audit Log", values(audit))
    put(sh, "READ ME", readme(hl), header_fmt=False)
    dashboard(sh, hl)
    charts(sh)

    order = {t: i for i, t in enumerate(TABS)}
    sh.batch_update({"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": w.id, "index": order[w.title]}, "fields": "index"}}
        for w in sh.worksheets() if w.title in order]})
    for w in sh.worksheets():
        if w.title not in order:
            sh.del_worksheet(w)
    print("[OK] done:", sh.url)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else SHEET_ID)
