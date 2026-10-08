"""Snapshot tabs of the live Google Sheet WITHOUT a browser.

    python src/sheet_snapshots.py

Google renders the sheet itself (formats, status colours, charts) through the spreadsheet
PDF export endpoint; the PDF page is rasterised with PyMuPDF and the white margin trimmed.
Writes screens/sheet-dashboard.png and screens/sheet-inventory.png.
"""
from __future__ import annotations

import os

import fitz
import gspread
import requests
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from PIL import Image, ImageChops

from common import SCREENS
from to_gsheet import CREDS, SCOPES, SHEET_ID

SHOTS = [  # (tab, range, output, gridlines)
    ("Dashboard", "A1:L41", "sheet-dashboard.png", "false"),
    ("Inventory", "A1:Q42", "sheet-inventory.png", "true"),
]


def export_pdf(token: str, gid: int, rng: str, gridlines: str) -> bytes:
    params = {"format": "pdf", "gid": gid, "range": rng, "portrait": "false", "fitw": "true",
              "size": "A4", "gridlines": gridlines, "sheetnames": "false", "printtitle": "false",
              "pagenum": "UNDEFINED", "top_margin": "0.2", "bottom_margin": "0.2",
              "left_margin": "0.2", "right_margin": "0.2"}
    r = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export", params=params,
                     headers={"Authorization": f"Bearer {token}"}, timeout=120)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"export did not return a PDF (got {r.headers.get('content-type')})")
    return r.content


def pdf_to_png(pdf: bytes, path: str, zoom: float = 2.5) -> tuple[int, int]:
    page = fitz.open(stream=pdf, filetype="pdf")[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    bbox = ImageChops.difference(img, Image.new("RGB", img.size, "white")).getbbox()
    if bbox:
        pad = 12
        img = img.crop((max(bbox[0] - pad, 0), max(bbox[1] - pad, 0),
                        min(bbox[2] + pad, img.width), min(bbox[3] + pad, img.height)))
    img.save(path)
    return img.size


def main() -> None:
    creds = Credentials.from_service_account_file(CREDS, scopes=SCOPES)
    creds.refresh(Request())
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)
    os.makedirs(SCREENS, exist_ok=True)
    for tab, rng, out, grid in SHOTS:
        pdf = export_pdf(creds.token, sh.worksheet(tab).id, rng, grid)
        size = pdf_to_png(pdf, os.path.join(SCREENS, out))
        print(f"[OK] {tab} {rng} -> screens/{out} {size[0]}x{size[1]}")


if __name__ == "__main__":
    main()
