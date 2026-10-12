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
    the event, then on the market slug's tail.
    """
    if not slug:
        return None
    for us_slug in _us_event(event_slug):
        if us_slug == slug or us_slug.endswith("-" + slug):
            return us_slug
    return None


def has_keys() -> bool:
    return bool(os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY"))


def _client():
    from polymarket_us import PolymarketUS  # imported lazily so paper mode needs no SDK

    return PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"], secret_key=os.environ["POLYMARKET_SECRET_KEY"])


@lru_cache(maxsize=2000)
def _sides(us_slug: str) -> tuple:
    """((name, is_long), ...) for each side of a Polymarket US market."""
    try:
        r = requests.get(f"{GATEWAY}/v1/market/slug/{us_slug}", timeout=15)
        m = (r.json() or {}).get("market") or {} if r.ok else {}
    except (requests.RequestException, ValueError):
        return ()
    out = []
    for side in m.get("marketSides") or []:
        names = {str(side.get("description") or "").lower(), str((side.get("team") or {}).get("name") or "").lower()}
        out.append((tuple(n for n in names if n), bool(side.get("long"))))
    return tuple(out)


def is_long(us_slug: str, outcome: str, outcome_index: int) -> bool:
    """Whether the international outcome we copy is the long (YES) side on Polymarket US.

    US markets are one long and one short side, and their order doesn't always match
    the international outcome order, so match on the outcome's name first.
    """
    name = str(outcome or "").strip().lower()
    if name in ("yes", "no"):
        return name == "yes"
    for names, long in _sides(us_slug):
        if any(name and (name == n or name in n or n in name) for n in names):
            return long
    return int(outcome_index or 0) == 0


def quote(us_slug: str, long: bool) -> Tuple[Optional[float], Optional[float]]:
    """(best bid, best ask) for one side of a Polymarket US market, from the public gateway.

    The book is quoted for the long side; the short side is its mirror image
    (buying NO at 1 - the best YES bid).
    """
    try:
        r = requests.get(f"{GATEWAY}/v1/markets/{us_slug}/bbo", timeout=10)
        d = (r.json() or {}).get("marketData") or {} if r.ok else {}
    except (requests.RequestException, ValueError):
        return None, None
    if d.get("state") not in (None, "MARKET_STATE_OPEN"):
        return None, None

    def px(key):
        try:
            v = float((d.get(key) or {}).get("value"))
        except (TypeError, ValueError):
            return None
        return v if 0 < v < 1 else None

    bid, ask = px("bestBid"), px("bestAsk")
    if long:
        return bid, ask
    return (round(1 - ask, 4) if ask else None), (round(1 - bid, 4) if bid else None)


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
    """(shares, average price) actually executed from an order response."""
    shares = cost = 0.0
    for ex in (resp or {}).get("executions") or []:
        try:
            q = float(ex.get("lastShares") or 0)
            px = float((ex.get("lastPx") or {}).get("value") or 0)
        except (TypeError, ValueError):
            continue
        shares += q
        cost += q * px
    return shares, (cost / shares if shares else 0.0)


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
