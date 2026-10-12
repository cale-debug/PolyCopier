"""Offline tests: copy rules, paper fills, and the loop against fake market data."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import live_us, main, polymarket as pm  # noqa: E402
from bot.portfolio import DEFAULT_SETTINGS, bet_size, buy, new_state, sell, should_copy_buy  # noqa: E402
from bot.store import LocalStore  # noqa: E402

TRADE = {"asset": "A", "title": "Will X?", "outcome": "Yes", "slug": "will-x", "price": 0.40,
         "usdcSize": 400, "size": 1000, "side": "BUY", "timestamp": 100, "transactionHash": "0x1"}


def test_bet_size_modes():
    assert bet_size({"size_mode": "5"}, 999) == 5
    assert bet_size({"size_mode": "prop", "prop_pct": 1.0}, 250) == 2.5


def test_skip_rules():
    assert should_copy_buy({"min_his_bet": 100}, 50) == "below min bet"
    assert should_copy_buy({"min_his_bet": 100}, 150) is None
    assert should_copy_buy({"halted": True}, 1e6) == "halted"


def test_buy_sell_and_win_loss():
    st = new_state(100)
    buy(st, TRADE, 10, 0.50, 3)
    assert round(st["bank"], 2) == 90 and round(st["positions"]["A"]["shares"], 4) == 20
    sell(st, "A", 0.5, 0.60, "copied sell")       # +1.00 on half
    assert round(st["realized"], 2) == 1.0 and st["wins"] == 0  # still open
    sell(st, "A", 1.0, 0.0, "resolved")            # -5.00 on the rest
    assert "A" not in st["positions"]
    assert round(st["realized"], 2) == -4.0 and st["losses"] == 1
    assert round(st["bank"], 2) == 96.0


def test_buy_never_spends_more_than_bank():
    st = new_state(3)
    buy(st, TRADE, 5, 0.5, 0)
    assert st["bank"] == 0 and round(st["positions"]["A"]["cost"], 2) == 3


class Fake:
    def __init__(self):
        self.trades = []

    def new_trades(self, wallet, since):
        return [t for t in self.trades if t["timestamp"] >= since]


def test_loop_copies_buys_and_sells(tmp_path, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(pm, "new_trades", fake.new_trades)
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(pm, "his_position_shares", lambda w, a: 500.0)
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug if slug == "will-x" else None)
    store = LocalStore(str(tmp_path))
    bot = main.Bot(store)
    bot.poll()                                   # first poll pins the cursor to "now"
    now = bot.state["cursor"]["ts"]
    fake.trades = [
        dict(TRADE, timestamp=now + 1),
        dict(TRADE, timestamp=now + 2, transactionHash="0x2", slug="elsewhere", usdcSize=50),
        dict(TRADE, timestamp=now + 3, transactionHash="0x3", side="SELL", size=500),
    ]
    bot.settings["min_his_bet"] = 100
    bot.poll()
    bot.poll()                                   # replays must not double-copy
    buys = [t for t in bot.state["trades"] if t["side"] == "BUY"]
    sells = [t for t in bot.state["trades"] if t["side"] == "SELL"]
    assert len(buys) == 1 and buys[0]["price"] == 0.42
    assert bot.state["skipped"] == {"below min bet": 1}  # checked before the US filter
    assert bot.state["us"] == {"checked": 2, "available": 1}
    # He sold 500 and has 500 left, so we sell half at the bid.
    assert len(sells) == 1 and abs(bot.state["positions"]["A"]["shares"] - (1 / 0.42) / 2) < 1e-9


def test_kill_switch_and_target_change(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.30, 0.35))
    monkeypatch.setattr(pm, "new_trades", lambda w, s: [])
    store = LocalStore(str(tmp_path))
    bot = main.Bot(store)
    buy(bot.state, TRADE, 10, 0.5, 0)
    store.write("settings.json", {**DEFAULT_SETTINGS, "kill": True, "target": "0xabc"}, "")
    bot.refresh_settings()
    assert bot.state["positions"] == {} and bot.settings["halted"] is True
    assert store.read("settings.json")[0]["kill"] is False
    bot.poll()
    assert bot.state["cursor"]["target"] == "0xabc"


def test_order_fill_parsing():
    resp = {"executions": [{"lastShares": "10", "lastPx": {"value": "0.40"}},
                           {"lastShares": "5", "lastPx": {"value": "0.43"}}]}
    shares, px = live_us.filled(resp)
    assert shares == 15 and abs(px - 0.41) < 1e-9


def test_us_only_skips_markets_missing_on_polymarket_us(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": None)
    bot = main.Bot(LocalStore(str(tmp_path)))
    bot.copy(dict(TRADE), 5)
    assert bot.state["positions"] == {} and bot.state["skipped"] == {"not on Polymarket US": 1}
    bot.settings["us_only"] = False
    bot.copy(dict(TRADE), 5)
    assert "A" in bot.state["positions"]


def test_us_market_matches_prefixed_slug_in_same_event(monkeypatch):
    live_us._us_event.cache_clear()
    monkeypatch.setattr(live_us, "_us_event", lambda ev: ("atc-epl-cov-new-2026-10-12-cov", "atc-epl-cov-new-2026-10-12-draw")
                        if ev == "epl-cov-new-2026-10-12" else ())
    assert live_us.us_market("epl-cov-new-2026-10-12-cov", "epl-cov-new-2026-10-12") == "atc-epl-cov-new-2026-10-12-cov"
    assert live_us.us_market("epl-cov-new-2026-10-12-total-3pt5", "epl-cov-new-2026-10-12") is None
    assert live_us.us_market("x", "other-event") is None


def test_once_per_market_and_both_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    bot = main.Bot(LocalStore(str(tmp_path)))
    bot.settings.update(once_per_market=True, skip_both_sides=True)
    t = dict(TRADE, conditionId="m1")
    bot.copy(t, 1)
    bot.copy(t, 1)                                  # another fill of the same buy
    bot.copy(dict(t, asset="B", outcome="No"), 1)   # the other side of the same market
    assert list(bot.state["positions"]) == ["A"]
    assert bot.state["skipped"] == {"already holding": 1, "other side of a market we hold": 1}
