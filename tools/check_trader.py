"""Size up a wallet before copying it: style, red flags, and Polymarket US coverage.

    python tools/check_trader.py 0xWALLET
"""
import statistics as st
import sys
from collections import Counter, defaultdict

import requests

D = "https://data-api.polymarket.com"
US = "https://gateway.polymarket.us"
W = sys.argv[1].lower()
f = lambda v: float(v or 0)


def get(url, params=None):
    r = requests.get(url, params=params, timeout=30)
    return r.json() if r.ok else None


def rows(x):
    return x if isinstance(x, list) else (x or {}).get("data") or []


acts = []
for off in (0, 500, 1000):
    acts += rows(get(f"{D}/activity", {"user": W, "type": "TRADE", "limit": 500, "offset": off,
                                       "sortBy": "TIMESTAMP", "sortDirection": "DESC"}))
if not acts:
    sys.exit("no trades found")
buys = [a for a in acts if a.get("side") == "BUY"]
sells = [a for a in acts if a.get("side") == "SELL"]
ts = [int(a["timestamp"]) for a in acts]
days = max((max(ts) - min(ts)) / 86400, 1 / 24)
print(f"## {W}\n")
print(f"- last {len(acts)} trades span {days:.1f} days -> {len(acts)/days:.0f} trades/day; {len(buys)} buys, {len(sells)} sells")
if buys:
    usd = [f(b.get("usdcSize")) for b in buys]
    pr = [f(b.get("price")) for b in buys]
    print(f"- buy size: median ${st.median(usd):.2f}, mean ${st.mean(usd):.2f}, max ${max(usd):,.0f}")
    print(f"- buy price: median {st.median(pr):.3f}; >=90c {sum(p>=.9 for p in pr)/len(pr):.0%}; <=10c {sum(p<=.1 for p in pr)/len(pr):.0%}")
sell_ts = defaultdict(list)
for s in sells:
    sell_ts[s["asset"]].append(int(s["timestamp"]))
flip = sum(any(0 < x - int(b["timestamp"]) <= 3600 for x in sell_ts[b["asset"]]) for b in buys)
oc = defaultdict(set)
for b in buys:
    oc[b["conditionId"]].add(b["asset"])
print(f"- red flags: sold within 1h of buying {flip/max(1,len(buys)):.0%}; bought both sides in {sum(len(v)>1 for v in oc.values())}/{len(oc)} markets")

cats = Counter()
for b in buys:
    s = (b.get("eventSlug") or b.get("slug") or "").split("-")[0]
    cats[s] += 1
print(f"- slug prefixes (sport/league or topic): {cats.most_common(12)}")

# Polymarket US coverage: try the slug as-is, with the "aec-" prefix US uses for games, then search by title.
def on_us(b):
    slug = b.get("slug") or ""
    for cand in (slug, f"aec-{slug}"):
        if cand and get(f"{US}/v1/market/slug/{cand}"):
            return cand
    return None

sample = buys[:150]
hits = Counter()
matched = []
for b in sample:
    m = on_us(b)
    hits[bool(m)] += 1
    if m and len(matched) < 8:
        matched.append((b.get("title"), b.get("outcome"), m))
print(f"- on Polymarket US by slug: {hits[True]}/{len(sample)} of recent buys")
for t in matched:
    print(f"  - {t}")
print("- sample titles:")
for b in buys[:15]:
    print(f"  - {b.get('title')} | {b.get('outcome')} @ {f(b.get('price')):.2f} ${f(b.get('usdcSize')):.0f} | {b.get('slug')}")

cp = rows(get(f"{D}/closed-positions", {"user": W, "limit": 50, "sortBy": "TIMESTAMP", "sortDirection": "DESC"}))
pn = [f(p.get("realizedPnl")) for p in cp]
if pn:
    print(f"- last {len(pn)} closed positions: {sum(x>0 for x in pn)/len(pn):.0%} won, net ${sum(pn):,.0f}, worst {[round(x) for x in sorted(pn)[:3]]}")
for per in ("DAY", "WEEK", "MONTH", "ALL"):
    lb = rows(get(f"{D}/v1/leaderboard", {"timePeriod": per, "orderBy": "PNL", "user": W}))
    if lb:
        print(f"- leaderboard {per}: rank {lb[0].get('rank')}, P&L ${f(lb[0].get('pnl')):,.0f}, volume ${f(lb[0].get('vol')):,.0f}, name {lb[0].get('userName')}")
