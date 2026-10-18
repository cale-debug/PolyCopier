"""Paper portfolio and copy rules. Pure logic, no network, so it is easy to test.

State is one JSON-able dict so it can be saved to the repo as state.json and
read by the dashboard as-is.
"""

from __future__ import annotations

import logging
import time
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Optional

MAX_TRADES_KEPT = 400
MAX_CLOSED_KEPT = 500
MAX_EQUITY_POINTS = 3000
TAKER_FEE = 0.0695  # Polymarket US standard taker fee coefficient (docs.polymarket.us/fees)

DEFAULT_SETTINGS = {
    "target": "0x4f1d5ae26fc31472966e951af3183308736d8de2",  # e46m3
    "target_label": "e46m3",
    "size_mode": "1",          # "1" | "2" | "5" | "prop"
    "prop_pct": 1.0,           # PROP: copy this % of his dollar size
    "min_his_bet": 0,          # skip his buys smaller than this ($)
    "block_leagues": "",       # comma list of league prefixes to never buy (e.g. "mls")
    "us_only": True,           # only copy markets that also trade on Polymarket US
    "once_per_market": False,  # True: copy his first buy of an outcome, not every fill after it
    "skip_both_sides": False,  # True: don't buy the other side of a market we already hold
    "copy_sells": True,
    "max_slip_cents": 1.0,     # skip a buy if we'd pay more than this many cents above his price
    "max_delay_s": 60,         # skip a buy we only see this many seconds after he made it
    "max_spread_cents": 5.0,   # skip when the US book's bid/ask spread is wider than this
    "min_depth_usd": 20.0,     # skip when less than this many dollars are offered at the best ask
    "halted": False,
    "kill": False,             # one-shot: sell everything, then halt
    "mode": "paper",           # "paper" | "live"
    "live_max_trade": 5.0,     # live safety caps ($)
    "live_daily_cap": 50.0,
    "paper_start_bank": 3000.0,
    "max_exposure_pct": 50.0,  # never have more than this % of the account in open bets
    "max_drawdown_pct": 25.0,  # halt if the account falls this % below where it started
    "max_per_bet": 0.0,        # most we put into one outcome across all his entries (0 = no limit)
    "min_cash": 0.0,           # never let cash drop below this ($)
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
        "cursors": {},           # wallet -> {"ts", "seen"}: where we are in each trader's feed
        "traders": {},           # wallet -> {"label", "since", "buys", "sells", "usd", "pnl"}
        "heartbeat": 0,
        "live_spent_today": {"day": "", "usd": 0.0},
        "log": [],
        "fees": 0.0,
        "closed": [],            # one row per fully closed position, newest first, with its costs
        "totals": {},            # running cost breakdown; see add_total()
        "slip": {"n": 0, "cents": 0.0, "skipped": 0},  # our buy price vs his, in cents
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


def taker_fee(shares: float, price: float, theta: float = TAKER_FEE) -> float:
    """Polymarket US taker fee: theta x contracts x p x (1 - p), banker's-rounded to the cent."""
    p = Decimal(str(price))
    raw = Decimal(str(theta)) * Decimal(str(round(shares, 6))) * p * (1 - p)
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def slip_ok(settings: dict, his_price: float, our_price: float) -> bool:
    """True if paying `our_price` is within the slippage cap over his price."""
    cap = settings.get("max_slip_cents")
    if cap is None or float(cap) < 0:  # -1 means no cap
        return True
    return our_price <= his_price + float(cap) / 100 + 1e-9


def track_slip(state: dict, his_price: float, our_price: float) -> None:
    sl = state.setdefault("slip", {"n": 0, "cents": 0.0, "skipped": 0})
    sl["n"] += 1
    sl["cents"] = round(sl["cents"] + (our_price - his_price) * 100, 3)


def category(trade: dict) -> str:
    """Rough market category from the slug's league prefix (mlb, nfl, epl, ...)."""
    slug = str(trade.get("eventSlug") or trade.get("slug") or "")
    return slug.split("-")[0] or "other"


def blocked_league(settings: dict, trade: dict, us_slug: Optional[str] = None) -> Optional[str]:
    """The blocked league this buy belongs to, if any. Checks the international slug's prefix
    and the Polymarket US slug's league part (atc-mls-... -> mls)."""
    raw = settings.get("block_leagues") or ""
    blocked = {x.strip().lower() for x in (raw if isinstance(raw, list) else str(raw).split(",")) if x.strip()}
    if not blocked:
        return None
    seen = {category(trade).lower()}
    if us_slug and "-" in us_slug:
        seen.add(us_slug.split("-")[1].lower())
    hit = blocked & seen
    return sorted(hit)[0] if hit else None


def add_total(state: dict, **amounts) -> None:
    """Running totals: gross_in (spent on contracts), gross_out (sale/settlement value),
    settled (settlement value), fees_in, fees_out, slip (dollars paid above his price)."""
    t = state.setdefault("totals", {})
    for k, v in amounts.items():
        t[k] = round(t.get(k, 0.0) + v, 6)


def skip(state: dict, reason: str) -> None:
    state["skipped"][reason] = state["skipped"].get(reason, 0) + 1
    logging.getLogger("bot").info("skip: %s", reason)


def _record(state: dict, **row) -> None:
    row.setdefault("ts", int(time.time()))
    state["trades"].insert(0, row)
    del state["trades"][MAX_TRADES_KEPT:]


def buy(state: dict, trade: dict, usd: float, price: float, delay: int,
        theta: float = TAKER_FEE, fee: Optional[float] = None, meta: Optional[dict] = None) -> Optional[dict]:
    """Spend `usd` on contracts at `price`, plus the taker fee. Returns the fill or None.

    `fee` is the exchange-reported fee when we have one; otherwise the published
    schedule is used. `meta` (source/destination ids, book snapshot) goes on the fill.
    """
    if not 0 < price < 1:
        skip(state, "bad price")
        return None
    est = taker_fee(usd / price, price, theta) if fee is None else fee
    usd = min(usd, state["bank"] - est)
    if usd < 0.01:
        skip(state, "no cash")
        return None
    shares = usd / price
    if fee is None:
        fee = taker_fee(shares, price, theta)
    asset = trade["asset"]
    his_price = float(trade.get("price") or 0)
    pos = state["positions"].get(asset) or {
        "asset": asset, "title": trade.get("title", ""), "outcome": trade.get("outcome", ""),
        "slug": trade.get("slug", ""), "event_slug": trade.get("eventSlug", ""),
        "condition_id": trade.get("conditionId", ""), "category": category(trade), "shares": 0.0, "cost": 0.0, "opened_at": int(time.time()),
    }
    pos["shares"] += shares
    pos["cost"] += usd + fee  # fees are part of what the position cost us
    pos["avg"] = pos["cost"] / pos["shares"]
    pos.setdefault("cur", price)
    pos["theta"] = theta
    slip = shares * (price - his_price) if his_price else 0.0
    for k, v in (("gross_in", usd), ("fees_in", fee), ("slip", slip), ("delay_sum", delay), ("fills", 1)):
        pos[k] = pos.get(k, 0.0) + v
    for key, val in (("who", (meta or {}).get("who")), ("wallets", (meta or {}).get("wallet"))):
        if val and val not in pos.setdefault(key, []):
            pos[key].append(val)  # every trader we copied into this position
    state["positions"][asset] = pos
    state["bank"] -= usd + fee
    state["fees"] = round(state.get("fees", 0.0) + fee, 2)
    add_total(state, gross_in=usd, fees_in=fee, slip=slip)
    fill = {"side": "BUY", "title": pos["title"], "outcome": pos["outcome"], "price": round(price, 4),
            "usd": round(usd, 2), "fee": fee, "shares": round(shares, 4), "his_price": trade.get("price"),
            "his_usd": round(float(trade.get("usdcSize") or 0), 2), "his_ts": trade.get("timestamp"),
            "delay": delay, **(meta or {})}
    _record(state, **fill)
    return fill


def sell(state: dict, asset: str, fraction: float, price: float, why: str,
         fee: Optional[float] = None) -> Optional[dict]:
    """Sell `fraction` of a position at `price` (1.0 or 0.0 for resolution).

    Sales pay the taker fee unless `fee` is given; settlement ("resolved") is free.
    """
    pos = state["positions"].get(asset)
    if not pos or pos["shares"] <= 0:
        return None
    fraction = max(0.0, min(1.0, fraction))
    shares = pos["shares"] * fraction
    cost = pos["cost"] * fraction
    if fee is None:
        fee = 0.0 if why == "resolved" else taker_fee(shares, price, pos.get("theta", TAKER_FEE))
    gross = shares * price
    proceeds = gross - fee
    state["fees"] = round(state.get("fees", 0.0) + fee, 2)
    add_total(state, gross_out=gross, fees_out=fee, **({"settled": gross} if why == "resolved" else {}))
    pnl = proceeds - cost
    pos["shares"] -= shares
    pos["cost"] -= cost
    pos["gross_out"] = pos.get("gross_out", 0.0) + gross
    pos["fees_out"] = pos.get("fees_out", 0.0) + fee
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
        _close(state, pos, total, why)
    else:
        pos["closed_pnl"] = pos.get("closed_pnl", 0.0) + pnl
    fill = {"side": "SELL" if why != "resolved" else "SETTLE", "title": pos["title"], "outcome": pos["outcome"],
            "price": round(price, 4), "usd": round(proceeds, 2), "fee": fee, "shares": round(shares, 4),
            "pnl": round(pnl, 2), "note": why}
    if pos.get("who"):
        fill["who"] = ", ".join(pos["who"])
    _record(state, **fill)
    return fill


def _close(state: dict, pos: dict, net: float, why: str) -> None:
    """Ledger row for a fully closed position: what it cost, every fee, slippage, and net result."""
    fills = int(pos.get("fills", 0)) or 1
    row = {
        "title": pos["title"], "outcome": pos["outcome"], "category": pos.get("category", "other"),
        "src": pos["asset"], "dst": pos.get("us_slug"), "opened": pos.get("opened_at"), "closed": int(time.time()),
        "gross_in": round(pos.get("gross_in", 0.0), 4), "gross_out": round(pos.get("gross_out", 0.0), 4),
        "fees_in": round(pos.get("fees_in", 0.0), 2), "fees_out": round(pos.get("fees_out", 0.0), 2),
        "slip": round(pos.get("slip", 0.0), 4), "delay": round(pos.get("delay_sum", 0.0) / fills, 1),
        "fills": fills, "partial": pos.get("partial", 0), "net": round(net, 4), "exit": why,
        "who": ", ".join(pos.get("who") or []),
    }
    row["gross"] = round(row["gross_out"] - row["gross_in"], 4)
    closed = state.setdefault("closed", [])
    closed.insert(0, row)
    del closed[MAX_CLOSED_KEPT:]


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
