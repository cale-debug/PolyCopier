"""Real orders on Polymarket US (the CFTC-regulated exchange US residents use).

The trader we copy trades on international Polymarket, so each copy has to be
matched to the same market on Polymarket US. We look it up by the market's
slug; markets that don't exist there are skipped and counted. Even in paper
mode the bot checks this, so the test shows how much of the trader's activity
a US account could actually follow.

Keys come from the POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY environment
variables, which the workflow fills from GitHub Secrets set by the Go Live tab.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional, Tuple

import requests

log = logging.getLogger("bot.us")
GATEWAY = "https://gateway.polymarket.us"


@lru_cache(maxsize=2000)
def _us_event(event_slug: str) -> tuple:
    """Market slugs in the Polymarket US event with this slug (US reuses event slugs)."""
    if not event_slug:
        return ()
    try:
        r = requests.get(f"{GATEWAY}/v1/events/slug/{event_slug}", timeout=15)
    except requests.RequestException:
        return ()
    if not r.ok:
        return ()
    body = r.json()
    event = body.get("event") or body
    return tuple(m.get("slug", "") for m in event.get("markets") or [])


def us_market(slug: str, event_slug: str = "") -> Optional[str]:
    """The Polymarket US market slug matching an international market, or None.

    US keeps the international event slug but prefixes each market slug with a
    type code (e.g. "atc-" for soccer results, "aec-" for US games), so match on
    the event, then on the market slug's tail. More than one candidate means the
    match is ambiguous, and we'd rather skip than guess.
    """
    if not slug:
        return None
    hits = [u for u in _us_event(event_slug) if u == slug or u.endswith("-" + slug)]
    return hits[0] if len(hits) == 1 else None


def side_for(us_slug: str, outcome: str) -> Optional[bool]:
    """True/False if the international outcome is the US market's long/short side; None if unsure.

    Yes/No markets map Yes to long. Team markets match the outcome name to exactly
    one side's name; anything else (no match, or both sides match) is None.
    """
    name = str(outcome or "").strip().lower()
    if not name:
        return None
    if name in ("yes", "no"):
        return name == "yes"
    sides = _sides(us_slug)
    exact = [long for names, long in sides if name in names]
    if len(exact) == 1:
        return exact[0]
    loose = [long for names, long in sides if any(name in n or n in name for n in names)]
    return loose[0] if len(loose) == 1 else None


def match(slug: str, event_slug: str, outcome: str) -> Tuple[Optional[str], Optional[bool], str]:
    """(US slug, is_long, "") for a verified same-event, same-date, same-outcome contract,
    or (None, None, reason) when there isn't one we're sure of."""
    us_slug = us_market(slug, event_slug)
    if not us_slug:
        return None, None, "not on Polymarket US"
    if _market(us_slug)[2]:
        return None, None, "US market closed"
    long = side_for(us_slug, outcome)
    if long is None:
        return None, None, "outcome doesn't match a US side"
    return us_slug, long, ""


def has_keys() -> bool:
    return bool(os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY"))


def _client():
    from polymarket_us import PolymarketUS  # imported lazily so paper mode needs no SDK

    return PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"], secret_key=os.environ["POLYMARKET_SECRET_KEY"])


@lru_cache(maxsize=2000)
def _market(us_slug: str) -> tuple:
    """(sides, fee coefficient, closed) of a Polymarket US market; sides are ((names), is_long) pairs."""
    try:
        r = requests.get(f"{GATEWAY}/v1/market/slug/{us_slug}", timeout=15)
        m = (r.json() or {}).get("market") or {} if r.ok else {}
    except (requests.RequestException, ValueError):
        return (), None, False
    sides = []
    for side in m.get("marketSides") or []:
        names = {str(side.get("description") or "").lower(), str((side.get("team") or {}).get("name") or "").lower()}
        sides.append((tuple(n for n in names if n), bool(side.get("long"))))
    try:
        theta = float(m["feeCoefficient"])
    except (KeyError, TypeError, ValueError):
        theta = None
    closed = bool(m.get("closed")) or m.get("status") not in (None, "MARKET_STATUS_OPEN")
    return tuple(sides), theta, closed


def _sides(us_slug: str) -> tuple:
    return _market(us_slug)[0]


def fee_coefficient(us_slug: str) -> float:
    """Taker fee coefficient for this market (0.0695 for most; table tennis is higher)."""
    from .portfolio import TAKER_FEE
    theta = _market(us_slug)[1] if us_slug else None
    return TAKER_FEE if theta is None else theta


def settlement(us_slug: str, long: bool) -> Optional[float]:
    """1.0 / 0.0 for our side once the Polymarket US market has resolved, else None.

    US markets settle on their own schedule, often hours before the international
    market finishes its resolution process, and this is where real money would settle.
    """
    try:
        r = requests.get(f"{GATEWAY}/v1/market/slug/{us_slug}", timeout=15)
        m = (r.json() or {}).get("market") or {} if r.ok else {}
    except (requests.RequestException, ValueError):
        return None
    if m.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    for side in m.get("marketSides") or []:
        if bool(side.get("long")) == bool(long):
            try:
                px = float(side.get("price"))
            except (TypeError, ValueError):
                return None
            return px if px in (0.0, 1.0) else None
    return None


def is_long(us_slug: str, outcome: str, outcome_index: int) -> bool:
    """Best-effort side for positions opened before strict matching; new copies use match()."""
    side = side_for(us_slug, outcome)
    return (int(outcome_index or 0) == 0) if side is None else side


def book(us_slug: str, long: bool) -> Optional[dict]:
    """Top of the Polymarket US book for one side: bid, ask, spread and resting shares at each.

    The gateway quotes the long side; the short side is its mirror image (buying NO
    at 1 - the best YES bid, against the shares resting on that bid).
    """
    try:
        r = requests.get(f"{GATEWAY}/v1/markets/{us_slug}/bbo", timeout=10)
        d = (r.json() or {}).get("marketData") or {} if r.ok else {}
    except (requests.RequestException, ValueError):
        return None
    if d.get("state") not in (None, "MARKET_STATE_OPEN"):
        return None

    def num(key, sub=True):
        try:
            v = float((d.get(key) or {}).get("value") if sub else d.get(key))
        except (TypeError, ValueError):
            return None
        return v

    bid, ask = num("bestBid"), num("bestAsk")
    bid = bid if bid and 0 < bid < 1 else None
    ask = ask if ask and 0 < ask < 1 else None
    bid_sh, ask_sh = num("bidShares", False) or 0.0, num("askShares", False) or 0.0
    if not long:
        bid, ask, bid_sh, ask_sh = (round(1 - ask, 4) if ask else None), (round(1 - bid, 4) if bid else None), ask_sh, bid_sh
    spread = round(ask - bid, 4) if bid is not None and ask is not None else None
    return {"bid": bid, "ask": ask, "bid_shares": bid_sh, "ask_shares": ask_sh, "spread": spread}


def quote(us_slug: str, long: bool) -> Tuple[Optional[float], Optional[float]]:
    """(best bid, best ask) for one side of a Polymarket US market."""
    b = book(us_slug, long)
    return (b["bid"], b["ask"]) if b else (None, None)


def place(slug: str, long: bool, action: str, qty: float, limit: float) -> dict:
    """Immediate-or-cancel limit order: fills at `limit` or better, or not at all."""
    order = {
        "marketSlug": slug,
        "type": "ORDER_TYPE_LIMIT",
        "outcomeSide": "OUTCOME_SIDE_YES" if long else "OUTCOME_SIDE_NO",
        "action": "ORDER_ACTION_BUY" if action == "BUY" else "ORDER_ACTION_SELL",
        "price": {"value": f"{limit:.3f}", "currency": "USD"},
        "quantity": round(qty, 2),
        "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "synchronousExecution": True,
        "maxBlockTime": "5",
    }
    client = _client()
    try:
        return client.orders.create(order)
    finally:
        client.close()


def filled(resp: dict) -> tuple:
    """(shares, average price, fee or None) actually executed from an order response.

    The fee is what the exchange reported on the executions, when it reports one;
    None means the caller should fall back to the published fee schedule.
    """
    shares = cost = 0.0
    fee = None
    for ex in (resp or {}).get("executions") or []:
        try:
            q = float(ex.get("lastShares") or 0)
            px = float((ex.get("lastPx") or {}).get("value") or 0)
        except (TypeError, ValueError):
            continue
        shares += q
        cost += q * px
        f = ex.get("fee") or ex.get("commission")
        if isinstance(f, dict):
            f = f.get("value")
        try:
            fee = (fee or 0.0) + abs(float(f)) if f is not None else fee
        except (TypeError, ValueError):
            pass
    return shares, (cost / shares if shares else 0.0), fee


def balance() -> Optional[float]:
    client = _client()
    try:
        b = client.account.balances()
        rows = b.get("balances") or [b]
        return float(rows[0].get("buyingPower") or rows[0].get("currentBalance") or 0)
    except Exception as exc:
        log.warning("Balance check failed: %s", exc)
        return None
    finally:
        client.close()
