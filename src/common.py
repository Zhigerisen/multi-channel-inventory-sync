"""Shared paths, the rules loader and the inventory-planning formulas.

Formulas (standard inventory theory):
    safety stock   = Z * sigma_daily * sqrt(lead_time)
    reorder point  = avg_daily * lead_time + safety stock
    EOQ            = sqrt(2 * D * S / H)   D = annual units, S = cost per order, H = holding cost per unit-year
"""
from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import yaml

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
SCREENS = os.path.join(HERE, "screens")
RULES_PATH = os.path.join(HERE, "rules.yaml")

DAY = 86400.0


def load_rules(path: str = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def start_dt(rules: dict) -> datetime:
    return datetime.fromisoformat(rules["start_date"])


def channel_table(rules: dict) -> list[tuple[str, str, float, float, float]]:
    """[(id, display name, order share, push latency sec, offer cap)] in config order."""
    return [(cid, v[0], float(v[1]), float(v[2]), float(v[3]))
            for cid, v in rules["channels"].items()]


def safety_stock(sigma: np.ndarray, lead: np.ndarray, z: float) -> np.ndarray:
    return z * sigma * np.sqrt(lead)


def reorder_point(avg: np.ndarray, sigma: np.ndarray, lead: np.ndarray, z: float) -> np.ndarray:
    return np.ceil(avg * lead + safety_stock(sigma, lead, z)).astype(np.int64)


def eoq(avg: np.ndarray, unit_cost: np.ndarray, order_cost: float, holding_rate: float) -> np.ndarray:
    annual = avg * 365.0
    holding = np.maximum(unit_cost * holding_rate, 0.01)
    q = np.sqrt(2.0 * annual * order_cost / holding)
    return np.where(annual > 0, np.maximum(np.ceil(q), 1), 0).astype(np.int64)


def pct(a: float, b: float) -> float:
    return 0.0 if b == 0 else round(100.0 * a / b, 1)
