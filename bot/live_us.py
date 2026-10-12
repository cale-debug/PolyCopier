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
from typing import Optional

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


def _side(outcome_index: int) -> str:
    # Polymarket US markets are binary: the first outcome is the long (YES) side.
    return "OUTCOME_SIDE_YES" if int(outcome_index or 0) == 0 else "OUTCOME_SIDE_NO"


def place(slug: str, outcome_index: int, action: str, usd: float, price: float) -> dict:
    """Immediate-or-cancel limit order, at most 2 cents worse than `price`."""
    limit = min(0.99, price + 0.02) if action == "BUY" else max(0.01, price - 0.02)
    qty = round(usd / limit, 2)
    order = {
        "marketSlug": slug,
        "type": "ORDER_TYPE_LIMIT",
        "outcomeSide": _side(outcome_index),
        "action": "ORDER_ACTION_BUY" if action == "BUY" else "ORDER_ACTION_SELL",
        "price": {"value": f"{limit:.3f}", "currency": "USD"},
        "quantity": qty,
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


def sell_qty(slug: str, outcome_index: int, shares: float, price: float) -> dict:
    return place(slug, outcome_index, "SELL", shares * max(price - 0.02, 0.01), price)


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
