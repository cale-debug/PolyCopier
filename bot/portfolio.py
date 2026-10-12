"""Paper portfolio and copy rules. Pure logic, no network, so it is easy to test.

State is one JSON-able dict so it can be saved to the repo as state.json and
read by the dashboard as-is.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

MAX_TRADES_KEPT = 400
MAX_EQUITY_POINTS = 3000

DEFAULT_SETTINGS = {
    "target": "0x4f1d5ae26fc31472966e951af3183308736d8de2",  # e46m3
    "target_label": "e46m3",
    "size_mode": "1",          # "1" | "2" | "5" | "prop"
    "prop_pct": 1.0,           # PROP: copy this % of his dollar size
    "min_his_bet": 0,          # skip his buys smaller than this ($)
    "us_only": True,           # only copy markets that also trade on Polymarket US
    "copy_sells": True,
    "halted": False,
    "kill": False,             # one-shot: sell everything, then halt
    "mode": "paper",           # "paper" | "live"
    "live_max_trade": 5.0,     # live safety caps ($)
    "live_daily_cap": 50.0,
    "paper_start_bank": 1000.0,
    "reset_paper": False,      # one-shot: wipe paper history and start over
}


def new_state(bank: float) -> dict:
    return {
        "version": 1,
        "started_at": int(time.time()),
        "start_bank": bank,
        "bank": bank,            # cash
        "realized": 0.0,
        "wins": 0,
        "losses": 0,
        "positions": {},         # asset id -> position
        "trades": [],            # newest first
        "skipped": {},
        "us": {"checked": 0, "available": 0},
        "equity": [],            # [ts, equity]
        "cursor": {"target": "", "ts": 0, "seen": []},
        "heartbeat": 0,
        "live_spent_today": {"day": "", "usd": 0.0},
        "log": [],
    }


def bet_size(settings: dict, his_usd: float) -> float:
    mode = str(settings.get("size_mode", "1"))
    if mode == "prop":
        return round(his_usd * float(settings.get("prop_pct", 1.0)) / 100, 2)
    try:
        return float(mode)
    except ValueError:
        return 1.0


def should_copy_buy(settings: dict, his_usd: float) -> Optional[str]:
    """None if we copy; otherwise the reason we skip."""
    if settings.get("halted"):
        return "halted"
    if his_usd < float(settings.get("min_his_bet", 0) or 0):
        return "below min bet"
    return None


def skip(state: dict, reason: str) -> None:
    state["skipped"][reason] = state["skipped"].get(reason, 0) + 1
    logging.getLogger("bot").info("skip: %s", reason)


def _record(state: dict, **row) -> None:
    row.setdefault("ts", int(time.time()))
    state["trades"].insert(0, row)
    del state["trades"][MAX_TRADES_KEPT:]


def buy(state: dict, trade: dict, usd: float, price: float, delay: int) -> Optional[dict]:
    """Open or add to a paper position at `price`. Returns the fill or None."""
    usd = min(usd, state["bank"])
    if usd < 0.01 or not 0 < price < 1:
        skip(state, "no cash" if usd < 0.01 else "bad price")
        return None
    shares = usd / price
    asset = trade["asset"]
    pos = state["positions"].get(asset) or {
        "asset": asset, "title": trade.get("title", ""), "outcome": trade.get("outcome", ""),
        "slug": trade.get("slug", ""), "condition_id": trade.get("conditionId", ""),
        "shares": 0.0, "cost": 0.0, "opened_at": int(time.time()),
    }
    pos["shares"] += shares
    pos["cost"] += usd
    pos["avg"] = pos["cost"] / pos["shares"]
    pos.setdefault("cur", price)
    state["positions"][asset] = pos
    state["bank"] -= usd
    fill = {"side": "BUY", "title": pos["title"], "outcome": pos["outcome"], "price": round(price, 4),
            "usd": round(usd, 2), "shares": round(shares, 4), "his_price": trade.get("price"),
            "his_usd": round(float(trade.get("usdcSize") or 0), 2), "delay": delay}
    _record(state, **fill)
    return fill


def sell(state: dict, asset: str, fraction: float, price: float, why: str) -> Optional[dict]:
    """Sell `fraction` of a position at `price` (1.0 or 0.0 for resolution)."""
    pos = state["positions"].get(asset)
    if not pos or pos["shares"] <= 0:
        return None
    fraction = max(0.0, min(1.0, fraction))
    shares = pos["shares"] * fraction
    cost = pos["cost"] * fraction
    proceeds = shares * price
    pnl = proceeds - cost
    pos["shares"] -= shares
    pos["cost"] -= cost
    state["bank"] += proceeds
    state["realized"] += pnl
    if pos["shares"] < 1e-6:
        del state["positions"][asset]
        # A position counts as a win/loss once it is fully closed.
        total = pos.get("closed_pnl", 0.0) + pnl
        if total >= 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
    else:
        pos["closed_pnl"] = pos.get("closed_pnl", 0.0) + pnl
    fill = {"side": "SELL" if why != "resolved" else "SETTLE", "title": pos["title"], "outcome": pos["outcome"],
            "price": round(price, 4), "usd": round(proceeds, 2), "shares": round(shares, 4),
            "pnl": round(pnl, 2), "note": why}
    _record(state, **fill)
    return fill


def exposure(state: dict) -> float:
    return sum(p["shares"] * p.get("cur", p.get("avg", 0)) for p in state["positions"].values())


def mark(state: dict) -> None:
    """Append an equity point (cash + marked positions), thinned to one per 10 min."""
    now = int(time.time())
    eq = round(state["bank"] + exposure(state), 2)
    if state["equity"] and now - state["equity"][-1][0] < 600:
        state["equity"][-1] = [now, eq]
    else:
        state["equity"].append([now, eq])
    del state["equity"][:-MAX_EQUITY_POINTS]


def log(state: dict, msg: str) -> None:
    state["log"].insert(0, [int(time.time()), msg])
    del state["log"][100:]
