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

import datetime
import itertools
import logging
import os
import re
import unicodedata
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


def match(slug: str, event_slug: str, outcome: str, title: str = "") -> Tuple[Optional[str], Optional[bool], str]:
    """(US slug, is_long, "") for a verified same-event, same-date, same-outcome contract,
    or (None, None, reason) when there isn't one we're sure of.

    First the direct route (US reuses the event slug and prefixes the market slug). When
    that finds nothing, translate: find the same game on US even if its slug uses other
    team codes, then the same bet in it (moneyline, spread at the same line, total at the
    same line, soccer "will X win"), and work out which US side is the same outcome.
    """
    us_slug = us_market(slug, event_slug)
    long = None
    if us_slug:
        long = side_for(us_slug, outcome)
    if us_slug is None or long is None:
        alt, alt_long = translate(slug, event_slug, outcome, title)
        if alt:
            us_slug, long = alt, alt_long
    if not us_slug:
        return None, None, "not on Polymarket US"
    if _market(us_slug)[2]:
        return None, None, "US market closed"
    if long is None:
        return None, None, "outcome doesn't match a US side"
    return us_slug, long, ""


# ---- translation: same game and same bet, different wording ----------------------
_MORE = "-more-markets"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore")
                  .decode().lower()).strip()


def _abbrev_of(short: str, long: str) -> bool:
    """'prv' for 'parivision': same first letter, letters in order."""
    if not (2 <= len(short) <= 5) or not long or short[0] != long[0]:
        return False
    it = iter(long)
    return all(c in it for c in short)


def same_team(a: str, b: str) -> bool:
    """Loose team-name equality: 'Rockets' ~ 'HOU Rockets', 'Washington State' ~ 'Washington State Cougars'."""
    ra, rb = str(a or "").strip(), str(b or "").strip()
    a, b = _norm(ra), _norm(rb)
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    if all(t in tb for t in ta) or all(t in ta for t in tb):
        return True
    # Esports handicap titles abbreviate in capitals ("PRV" for PARIVISION); only then allow letters-in-order.
    ca, cb = a.replace(" ", ""), b.replace(" ", "")
    return (ra.isupper() and len(ca) <= 4 and _abbrev_of(ca, cb)) or (rb.isupper() and len(cb) <= 4 and _abbrev_of(cb, ca))


def _teams(title: str) -> Tuple[str, ...]:
    t = re.sub(r"\s*-\s*more markets\s*$", "", str(title or ""), flags=re.I)
    t = re.sub(r"^[^:]{1,40}:\s*", "", t)          # "Counter-Strike: A vs B (BO3) - ..." -> "A vs B ..."
    t = re.sub(r"\s*\(.*$|\s+-\s+.*$", "", t)        # drop "(BO3) - tournament"
    parts = re.split(r"\s+vs\.?\s+", t, flags=re.I)
    return tuple(p.strip() for p in parts) if len(parts) == 2 and all(parts) else ()


def _date(slug: str) -> Optional[datetime.date]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", slug or "")
    try:
        return datetime.date.fromisoformat(m.group(1)) if m else None
    except ValueError:
        return None


@lru_cache(maxsize=2000)
def _intl_teams(event_slug: str) -> Tuple[str, ...]:
    """The two sides of an international event, from its title ("Warriors vs. Suns")."""
    try:
        r = requests.get("https://gamma-api.polymarket.com/events", params={"slug": event_slug}, timeout=15)
        rows = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return ()
    return _teams((rows[0] if rows else {}).get("title", ""))


@lru_cache(maxsize=2000)
def _search(query: str) -> Tuple[Tuple[str, str], ...]:
    """(slug, title) of Polymarket US events matching a search."""
    try:
        r = requests.get(f"{GATEWAY}/v1/search", params={"query": query}, timeout=15)
        evs = (r.json() or {}).get("events") or [] if r.ok else []
    except (requests.RequestException, ValueError):
        return ()
    return tuple((e.get("slug", ""), e.get("title", "")) for e in evs if e.get("slug"))


@lru_cache(maxsize=2000)
def _us_markets(event_slug: str) -> Tuple[Tuple[str, str], ...]:
    """(market slug, question) for every market in a Polymarket US event."""
    if not event_slug:
        return ()
    try:
        r = requests.get(f"{GATEWAY}/v1/events/slug/{event_slug}", timeout=15)
        body = r.json() if r.ok else {}
    except (requests.RequestException, ValueError):
        return ()
    event = body.get("event") or body
    return tuple((m.get("slug", ""), m.get("question") or "") for m in event.get("markets") or [])


@lru_cache(maxsize=2000)
def us_event(event_slug: str) -> Optional[str]:
    """The Polymarket US event for an international event slug, or None if not exactly one."""
    if not event_slug:
        return None
    base = event_slug[: -len(_MORE)] if event_slug.endswith(_MORE) else event_slug
    for cand in _slug_variants(event_slug, base):
        if _us_markets(cand):
            return cand
    teams, day, sport = _intl_teams(event_slug), _date(base), base.split("-")[0]
    if len(teams) != 2 or not day:
        return None
    found = set()
    for t in teams:
        words = sorted(_norm(t).split(), key=len, reverse=True)
        for q in dict.fromkeys([t] + words[:1]):
            for slug, title in _search(q):
                d = _date(slug)
                other = _teams(title)
                if (slug.startswith(sport + "-") and d and abs((d - day).days) <= 1 and len(other) == 2 and
                        (same_team(teams[0], other[0]) and same_team(teams[1], other[1]) or
                         same_team(teams[0], other[1]) and same_team(teams[1], other[0]))):
                    found.add(slug)
    return found.pop() if len(found) == 1 else None


def _slug_variants(event_slug: str, base: str) -> list:
    """The slug as-is, without "-more-markets", and with team codes' trailing digits dropped ("aur1" -> "aur")."""
    out = [event_slug, base]
    m = re.fullmatch(r"([a-z0-9]+)-(.+)-(\d{4}-\d{2}-\d{2})", base)
    if m:
        codes = m.group(2).split("-")
        options = [sorted({c, re.sub(r"(?<=[a-z])\d+$", "", c)}, key=len, reverse=True) for c in codes]
        for combo in itertools.product(*options):
            out.append("-".join([m.group(1), *combo, m.group(3)]))
    return list(dict.fromkeys(out))


def _line(code: str) -> float:
    return float(code.replace("pt", "."))


def _cover_team(question: str) -> str:
    m = re.match(r"\s*Will (?:the )?(.+?) cover", question or "", flags=re.I)
    return m.group(1) if m else ""


def _moneyline_side(question: str, outcome: str) -> Optional[bool]:
    """Moneyline sides list the first-named team as long: '... A vs B scheduled ...'."""
    m = re.search(r"event (.+?) vs\.? (.+?) scheduled", question or "", flags=re.I)
    if not m:
        return None
    a, b = same_team(outcome, m.group(1)), same_team(outcome, m.group(2))
    return True if a and not b else False if b and not a else None


def translate(slug: str, event_slug: str, outcome: str, title: str = "") -> Tuple[Optional[str], Optional[bool]]:
    """Same bet on the same game on Polymarket US, when the slugs don't line up. (slug, is_long) or (None, None)."""
    ev = us_event(event_slug)
    if not ev:
        return None, None
    markets = dict(_us_markets(ev))
    base = event_slug[: -len(_MORE)] if event_slug.endswith(_MORE) else event_slug
    tail = slug[len(base):] if slug.startswith(base) else None
    if tail is None:
        return None, None
    o = str(outcome or "").strip()
    yes_no = o.lower() in ("yes", "no")

    if tail == "":                                       # moneyline
        us = f"aec-{ev}"
        if us not in markets:
            return None, None
        side = side_for(us, o)
        return (us, side) if side is not None else (us, _moneyline_side(markets[us], o))

    m = re.fullmatch(r"-total-(\d+pt\d+)", tail)
    if m and o.lower() in ("over", "under"):
        hits = [u for u in (f"tsc-{ev}-{m.group(1)}", f"tsc-{ev}-tot-{m.group(1)}") if u in markets]
        return (hits[0], o.lower() == "over") if len(hits) == 1 else (None, None)

    m = re.fullmatch(r"-(spread|map-handicap)-(home|away)-(\d+pt\d+)", tail)
    if m and not yes_no:
        x = _line(m.group(3))
        # Which side gets -x? "Spread: Suns (-9.5)" names the favourite; "Map Handicap: PRV (-1.5) vs fnatic (+1.5)" both.
        fav = re.search(r"([^:()]+?)\s*\(-", title or "")
        dog = re.search(r"vs\.?\s+([^()]+?)\s*\(\+", title or "")
        if not fav:
            return None, None
        if same_team(o, fav.group(1).strip()) and not (dog and same_team(o, dog.group(1).strip())):
            mine = -x
        elif dog and same_team(o, dog.group(1).strip()) or not same_team(o, fav.group(1).strip()):
            mine = x
        else:
            return None, None
        others = [t for t in _intl_teams(event_slug) if not same_team(o, t)]
        infix = "hcap-" if m.group(1) == "map-handicap" else ""
        hits = []
        for sign, val in (("neg", -x), ("pos", x)):
            us = f"asc-{ev}-{infix}{sign}-{m.group(3)}"
            if us not in markets:
                continue
            named = _cover_team(markets[us])
            if same_team(o, named):
                if val == mine:
                    hits.append((us, True))
            elif named and val == -mine and (not others or any(same_team(named, t) for t in others)):
                hits.append((us, False))
        return hits[0] if len(hits) == 1 else (None, None)

    if yes_no and re.fullmatch(r"-[a-z0-9]+", tail):     # soccer "Will X win on ...?" / "... end in a draw?"
        draw = "draw" in (title or "").lower()
        win = re.match(r"\s*Will (.+?) win", title or "", flags=re.I)
        hits = []
        for us, q in markets.items():
            if not us.startswith(f"atc-{ev}-") or "-" in us[len(f"atc-{ev}-"):]:
                continue
            if draw and us.endswith("-draw") or win and not us.endswith("-draw") and \
                    same_team(win.group(1), (re.match(r"\s*Will (?:the )?(.+?) win", q, flags=re.I) or [None, ""])[1]):
                hits.append(us)
        return (hits[0], o.lower() == "yes") if len(hits) == 1 else (None, None)
    return None, None


def has_keys() -> bool:
    return bool(os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY"))


def _client():
    from polymarket_us import PolymarketUS  # imported lazily so paper mode needs no SDK

    return PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"], secret_key=os.environ["POLYMARKET_SECRET_KEY"])


@lru_cache(maxsize=2000)
def _market_raw(us_slug: str) -> dict:
    try:
        r = requests.get(f"{GATEWAY}/v1/market/slug/{us_slug}", timeout=15)
        return (r.json() or {}).get("market") or {} if r.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def order_rules(us_slug: str) -> Tuple[float, float]:
    """(price tick, minimum quantity) for orders on this market."""
    m = _market_raw(us_slug)
    try:
        tick = float(m.get("orderPriceMinTickSize") or 0.001)
    except (TypeError, ValueError):
        tick = 0.001
    try:
        min_qty = float(m.get("minimumTradeQty") or 1)
    except (TypeError, ValueError):
        min_qty = 1.0
    return tick, min_qty


@lru_cache(maxsize=2000)
def _market(us_slug: str) -> tuple:
    """(sides, fee coefficient, closed) of a Polymarket US market; sides are ((names), is_long) pairs."""
    m = _market_raw(us_slug)
    if not m:
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


def _round_order(us_slug: str, action: str, qty: float, limit: float) -> Tuple[float, float]:
    """Round to the market's tick and lot size, always in our favour: a buy limit
    rounds down and a sell limit rounds up, so we never pay past the cap."""
    import math
    tick, min_qty = order_rules(us_slug)
    steps = limit / tick
    steps = math.floor(steps + 1e-9) if action == "BUY" else math.ceil(steps - 1e-9)
    limit = min(1 - tick, max(tick, steps * tick))
    lot = min_qty if min_qty >= 1 else 0.01
    qty = math.floor(qty / lot + 1e-9) * lot
    return round(qty, 2), round(limit, 6)


def place(slug: str, long: bool, action: str, qty: float, limit: float) -> dict:
    """Immediate-or-cancel limit order: fills at `limit` or better, or not at all.

    `limit` is the price of the side we trade. Polymarket US prices every order in
    YES terms ("buy NO at 0.40" is sent as 0.60), so a NO order's limit is flipped."""
    qty, limit = _round_order(slug, action, qty, limit)
    if qty <= 0:
        return {"executions": [], "skipped": "order smaller than the market's minimum size"}
    order = {
        "marketSlug": slug,
        "type": "ORDER_TYPE_LIMIT",
        "outcomeSide": "OUTCOME_SIDE_YES" if long else "OUTCOME_SIDE_NO",
        "action": "ORDER_ACTION_BUY" if action == "BUY" else "ORDER_ACTION_SELL",
        "price": {"value": f"{(limit if long else 1 - limit):.4f}", "currency": "USD"},
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


def rejection(resp: dict) -> Optional[str]:
    """The exchange's reason when an order was rejected, if it gave one."""
    if (resp or {}).get("skipped"):
        return resp["skipped"]
    for ex in (resp or {}).get("executions") or []:
        if ex.get("type") == "EXECUTION_TYPE_REJECTED":
            return str(ex.get("orderRejectReason") or ex.get("text") or "rejected")
    return None


def filled(resp: dict, long: bool = True) -> tuple:
    """(shares, average price, fee or None) actually executed from an order response.

    Execution prices come back in YES terms; for a NO order the price we paid is 1 - that.

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
        f = ex.get("commissionNotionalCollected") or ex.get("fee") or ex.get("commission")
        if isinstance(f, dict):
            f = f.get("value")
        try:
            fee = (fee or 0.0) + abs(float(f)) if f is not None else fee
        except (TypeError, ValueError):
            pass
    px = cost / shares if shares else 0.0
    if shares and not long:
        px = 1 - px
    return shares, px, fee


def _amt(x) -> float:
    try:
        return float((x or {}).get("value") if isinstance(x, dict) else x)
    except (TypeError, ValueError):
        return 0.0


def account() -> Optional[dict]:
    """Cash and buying power straight from the Polymarket US account, or None if unreachable."""
    client = _client()
    try:
        b = client.account.balances()
        rows = b.get("balances") or [b]
        row = next((r for r in rows if r.get("currency") in (None, "USD")), rows[0])
        return {"cash": _amt(row.get("currentBalance")), "buying_power": _amt(row.get("buyingPower")),
                "positions_value": _amt(row.get("assetNotional")), "open_orders": _amt(row.get("openOrders"))}
    except Exception as exc:
        log.warning("Balance check failed: %s", exc)
        return None
    finally:
        client.close()


def balance() -> Optional[float]:
    a = account()
    return a["buying_power"] if a else None


def positions() -> Optional[dict]:
    """{US market slug: {"qty", "cost", "realized", "value", "outcome", "title", "expired"}} for the account."""
    client = _client()
    out, cursor = {}, None
    try:
        for page in range(50):
            try:
                resp = client.portfolio.positions(**({"cursor": cursor} if cursor else {}))
            except TypeError:
                if page == 0:
                    raise
                break  # SDK without paging: the first page is what we get
            for slug, p in (resp.get("positions") or {}).items():
                meta = p.get("marketMetadata") or {}
                out[slug] = {"qty": abs(_amt(p.get("netPositionDecimal") or p.get("netPosition"))),
                             "cost": _amt(p.get("cost")), "realized": _amt(p.get("realized")),
                             "value": _amt(p.get("cashValue")), "outcome": meta.get("outcome", ""),
                             "title": meta.get("title", ""), "expired": bool(p.get("expired"))}
            cursor = resp.get("nextCursor")
            if resp.get("eof") or not cursor:
                break
        return out
    except Exception as exc:
        log.warning("Positions check failed: %s", exc)
        return None
    finally:
        client.close()
