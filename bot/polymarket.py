"""Read-only Polymarket data: the target's trades, order books, resolutions."""

from __future__ import annotations

import json
import logging
import time
from typing import List, Optional, Tuple

import requests

DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

log = logging.getLogger("bot.pm")
_s = requests.Session()


def _get(url: str, params: dict):
    for attempt in range(3):
        try:
            r = _s.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            log.warning("GET %s failed: %s", url, exc)
            time.sleep(1 + attempt)
    return None


def _rows(x) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return x.get("data") or []
    return []


def new_trades(wallet: str, since_ts: int) -> List[dict]:
    """Target's TRADE activity at or after since_ts, oldest first."""
    out: List[dict] = []
    for offset in range(0, 2000, 500):
        rows = _rows(_get(f"{DATA}/activity", {
            "user": wallet, "type": "TRADE", "limit": 500, "offset": offset,
            "start": since_ts, "sortBy": "TIMESTAMP", "sortDirection": "DESC"}))
        out.extend(rows)
        if len(rows) < 500:
            break
    out = [r for r in out if int(r.get("timestamp") or 0) >= since_ts]
    out.sort(key=lambda r: int(r.get("timestamp") or 0))
    return out


def trade_key(t: dict) -> str:
    return f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('side')}:{t.get('size')}"


def best_prices(asset: str) -> Tuple[Optional[float], Optional[float]]:
    """(best bid, best ask) from the CLOB book."""
    book = _get(f"{CLOB}/book", {"token_id": asset})
    if not isinstance(book, dict):
        return None, None
    bids = [float(b["price"]) for b in book.get("bids") or []]
    asks = [float(a["price"]) for a in book.get("asks") or []]
    return (max(bids) if bids else None), (min(asks) if asks else None)


def midpoint(asset: str) -> Optional[float]:
    r = _get(f"{CLOB}/midpoint", {"token_id": asset})
    try:
        return float(r["mid"]) if r else None
    except (KeyError, TypeError, ValueError):
        return None


def resolution(asset: str) -> Optional[float]:
    """1.0 / 0.0 once the market holding `asset` has resolved, else None."""
    rows = _rows(_get(f"{GAMMA}/markets", {"clob_token_ids": asset}))
    if not rows:
        return None
    m = rows[0]
    if not m.get("closed"):
        return None
    try:
        ids = json.loads(m.get("clobTokenIds") or "[]")
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        price = prices[ids.index(asset)]
    except (ValueError, TypeError):
        return None
    if price >= 0.99:
        return 1.0
    if price <= 0.01:
        return 0.0
    return None


def his_position_shares(wallet: str, asset: str) -> float:
    rows = _rows(_get(f"{DATA}/positions", {"user": wallet, "asset": asset, "sizeThreshold": 0}))
    for r in rows:
        if str(r.get("asset")) == str(asset):
            return float(r.get("size") or 0)
    return 0.0


def resolve_target(text: str) -> Tuple[str, str]:
    """Accept a 0x wallet or a username; return (wallet, label)."""
    text = text.strip().lstrip("@")
    if text.startswith("0x") and len(text) == 42:
        return text.lower(), text[:8]
    r = _get(f"{GAMMA}/public-search", {"q": text, "search_profiles": "true", "limit_per_type": 5})
    for p in (r or {}).get("profiles") or []:
        if str(p.get("name", "")).lower() == text.lower() and p.get("proxyWallet"):
            return p["proxyWallet"].lower(), p["name"]
    return "", ""


def leaderboard_pnl(wallet: str) -> dict:
    """The wallet's P&L for DAY / MONTH / ALL from the public leaderboard."""
    out = {}
    for period in ("DAY", "MONTH", "ALL"):
        rows = _rows(_get(f"{DATA}/v1/leaderboard", {"timePeriod": period, "orderBy": "PNL", "user": wallet}))
        if rows:
            out[period.lower()] = round(float(rows[0].get("pnl") or 0), 2)
    return out
