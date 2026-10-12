"""Find consistently winning traders on the Polymarket leaderboard.

    python tools/scan_leaderboard.py --pool 150

Candidates must be profitable on the all-time, monthly and weekly boards. Each
is then measured on recent closed positions (win rate, profit per position,
worst loss), weekly consistency, trading style, and how much of what they buy
is also listed on Polymarket US. Prints a markdown table, best first.
"""
import argparse
import statistics as st
import time
from collections import defaultdict

import requests

D = "https://data-api.polymarket.com"
US = "https://gateway.polymarket.us"
S = requests.Session()
f = lambda v: float(v or 0)


def get(url, params=None):
    for attempt in range(3):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            return r.json() if r.ok else None
        except requests.RequestException:
            time.sleep(2)
    return None


def rows(x):
    return x if isinstance(x, list) else (x or {}).get("data") or []


CATEGORY = "OVERALL"


def board(period, n):
    out = {}
    for off in range(0, n, 50):
        for r in rows(get(f"{D}/v1/leaderboard", {"timePeriod": period, "orderBy": "PNL", "category": CATEGORY,
                                                  "limit": 50, "offset": off})):
            out[str(r.get("proxyWallet", "")).lower()] = r
    return out


_events = {}


def on_us(b):
    ev, slug = b.get("eventSlug") or "", b.get("slug") or ""
    if ev not in _events:
        body = get(f"{US}/v1/events/slug/{ev}") if ev else None
        event = (body or {}).get("event") or body or {}
        _events[ev] = [m.get("slug", "") for m in event.get("markets") or []]
    return any(s == slug or s.endswith("-" + slug) for s in _events[ev])


def score(w, name, lb):
    closed = []
    for off in range(0, 500, 50):
        page = rows(get(f"{D}/closed-positions", {"user": w, "limit": 50, "offset": off,
                                                  "sortBy": "TIMESTAMP", "sortDirection": "DESC"}))
        closed += page
        if len(page) < 50:
            break
    acts = rows(get(f"{D}/activity", {"user": w, "type": "TRADE", "limit": 500,
                                      "sortBy": "TIMESTAMP", "sortDirection": "DESC"}))
    buys = [a for a in acts if a.get("side") == "BUY"]
    if len(closed) < 20 or not buys:
        return None
    pnl = [f(p.get("realizedPnl")) for p in closed]
    cost = sum(f(p.get("totalBought")) * f(p.get("avgPrice")) for p in closed)
    weeks = defaultdict(float)
    for p in closed:
        weeks[int(f(p.get("timestamp")) // (7 * 86400))] += f(p.get("realizedPnl"))
    ts = [int(f(a.get("timestamp"))) for a in acts]
    span = max((max(ts) - min(ts)) / 86400, 1 / 24) if ts else 1
    oc = defaultdict(set)
    for b in buys:
        oc[b.get("conditionId")].add(b.get("asset"))
    sample = buys[:80]
    us = sum(on_us(b) for b in sample)
    cats = defaultdict(int)
    for b in buys:
        cats[(b.get("eventSlug") or "?").split("-")[0]] += 1
    top = ", ".join(k for k, _ in sorted(cats.items(), key=lambda x: -x[1])[:3])
    return {
        "wallet": w, "name": name or w[:10],
        "pnl_all": f(lb["ALL"].get("pnl")), "pnl_month": f(lb["MONTH"].get("pnl")), "pnl_week": f(lb["WEEK"].get("pnl")),
        "vol_all": f(lb["ALL"].get("vol")),
        "closed": len(closed), "win": sum(x > 0 for x in pnl) / len(pnl),
        "roi": sum(pnl) / cost if cost else 0, "worst": min(pnl),
        "weeks_green": sum(v > 0 for v in weeks.values()), "weeks": len(weeks),
        "per_day": len(acts) / span, "median_bet": st.median(f(b.get("usdcSize")) for b in buys),
        "median_price": st.median(f(b.get("price")) for b in buys),
        "both_sides": sum(len(v) > 1 for v in oc.values()) / max(1, len(oc)),
        "us": us / len(sample), "topics": top,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=150)
    ap.add_argument("--category", default="OVERALL", help="OVERALL, SPORTS, POLITICS, CRYPTO, ...")
    ap.add_argument("--need-week", action="store_true", help="also require a profitable week")
    args = ap.parse_args()
    global CATEGORY
    CATEGORY = args.category.upper()
    boards = {p: board(p, args.pool) for p in ("ALL", "MONTH", "WEEK")}
    need = ("ALL", "MONTH", "WEEK") if args.need_week else ("ALL", "MONTH")
    wallets = [w for w in boards["MONTH"] if all(w in boards[p] and f(boards[p][w].get("pnl")) > 0 for p in need)]
    print(f"{len(wallets)} wallets are profitable on the {CATEGORY} {' + '.join(need)} boards (top {args.pool} each)\n")
    out = []
    for i, w in enumerate(wallets, 1):
        r = score(w, boards["ALL"][w].get("userName"), {p: boards[p].get(w, {}) for p in boards})
        if r:
            out.append(r)
    # Consistency first: win rate and green weeks, then return on money risked.
    for r in out:
        r["rank_score"] = r["win"] * 50 + (r["weeks_green"] / max(1, r["weeks"])) * 30 + max(-20, min(20, r["roi"] * 100))
    out.sort(key=lambda r: -r["rank_score"])
    print("| # | Trader | Win rate | Green weeks | Return on $ risked | Worst loss | All-time P&L | Month P&L | Trades/day | Median bet | Median price | Both sides | On PM US | Topics | Wallet |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(out, 1):
        print(f"| {i} | {r['name']} | {r['win']:.0%} ({r['closed']}) | {r['weeks_green']}/{r['weeks']} | {r['roi']:+.1%} | "
              f"${r['worst']:,.0f} | ${r['pnl_all']:,.0f} | ${r['pnl_month']:,.0f} | {r['per_day']:,.0f} | ${r['median_bet']:,.0f} | "
              f"{r['median_price']:.2f} | {r['both_sides']:.0%} | {r['us']:.0%} | {r['topics']} | `{r['wallet']}` |")


if __name__ == "__main__":
    main()
