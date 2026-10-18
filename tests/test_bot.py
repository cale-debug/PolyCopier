"""Offline tests: copy rules, paper fills, and the loop against fake market data."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import live_us, main, polymarket as pm  # noqa: E402
from bot.portfolio import (DEFAULT_SETTINGS, bet_size, buy, new_state, sell, should_copy_buy,  # noqa: E402
                           taker_fee)
from bot.store import LocalStore  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def offline_us_book(monkeypatch):
    # Tests price US markets off the same fake book as international ones, and allow
    # the 2-cent gap between his 0.40 and the 0.42 ask unless a test says otherwise.
    def fake_book(slug, long):
        bid, ask = pm.best_prices(slug)
        return {"bid": bid, "ask": ask, "bid_shares": 1e4, "ask_shares": 1e4,
                "spread": round(ask - bid, 4) if bid and ask else None}
    monkeypatch.setattr(live_us, "book", fake_book)
    monkeypatch.setattr(live_us, "quote", lambda slug, long: pm.best_prices(slug))
    monkeypatch.setattr(live_us, "_market", lambda slug: ((), None, False))
    monkeypatch.setattr(pm, "his_position_shares", lambda w, a: 0.0)
    monkeypatch.setattr(live_us, "settlement", lambda slug, long: None)
    monkeypatch.setitem(DEFAULT_SETTINGS, "max_slip_cents", 5.0)

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
    buy(st, TRADE, 10, 0.50, 3, theta=0)           # fee-free here; fees have their own test
    assert round(st["bank"], 2) == 90 and round(st["positions"]["A"]["shares"], 4) == 20
    sell(st, "A", 0.5, 0.60, "copied sell", fee=0)  # +1.00 on half
    assert round(st["realized"], 2) == 1.0 and st["wins"] == 0  # still open
    sell(st, "A", 1.0, 0.0, "resolved")            # -5.00 on the rest
    assert "A" not in st["positions"]
    assert round(st["realized"], 2) == -4.0 and st["losses"] == 1
    assert round(st["bank"], 2) == 96.0


def test_buy_never_spends_more_than_bank():
    st = new_state(3)
    buy(st, TRADE, 5, 0.5, 0)
    assert 0 <= st["bank"] < 0.1 and round(st["positions"]["A"]["cost"] + st["bank"], 2) == 3


def test_fees_match_polymarket_us_examples():
    # Worked examples from docs.polymarket.us/fees (1,000 contracts each).
    assert taker_fee(1000, 0.10) == 6.26
    assert taker_fee(1000, 0.65) == 15.81
    assert taker_fee(1000, 0.30) == 14.60
    assert taker_fee(1000, 0.50) == 17.38
    assert taker_fee(1000, 0.50, 0.10) == 25.00
    # A $1 copy at 46c pays 4c, which counts against the position.
    st = new_state(100)
    buy(st, TRADE, 1, 0.46, 0)
    pos = st["positions"]["A"]
    assert st["fees"] == 0.04 and round(pos["cost"], 2) == 1.04 and round(st["bank"], 2) == 98.96
    sell(st, "A", 1.0, 1.0, "resolved")           # settlement is free
    assert st["fees"] == 0.04 and round(st["realized"], 2) == round(1 / 0.46 - 1.04, 2)


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
    now = bot.state["cursors"][bot.target()]["ts"]
    fake.trades = [
        dict(TRADE, timestamp=now + 1),
        dict(TRADE, timestamp=now + 2, transactionHash="0x2", asset="B", slug="elsewhere", usdcSize=50),
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
    # He bought 1000 and sold 500, so we sell half at the bid.
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
    assert list(bot.state["cursors"]) == ["0xabc"]


def test_follows_several_traders_and_tags_who(tmp_path, monkeypatch):
    feeds = {"0xa": [], "0xb": []}
    monkeypatch.setattr(pm, "new_trades", lambda w, since: [t for t in feeds[w] if t["timestamp"] >= since])
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "target": "0xA, 0xb", "target_label": "alice, bob",
                                  "size_mode": "5", "max_exposure_pct": 100}, "")
    bot = main.Bot(store)
    bot.state.pop("cursors"); bot.state["cursor"] = {"target": "0xa", "ts": 50, "seen": []}  # old single-trader state
    bot.poll()
    assert bot.state["cursors"]["0xa"]["ts"] == 50 and set(bot.state["traders"]) == {"0xa", "0xb"}
    now = bot.state["cursors"]["0xb"]["ts"]
    feeds["0xa"] = [dict(TRADE, timestamp=now + 1, asset="A", size=100)]
    feeds["0xb"] = [dict(TRADE, timestamp=now + 1, asset="B", transactionHash="0xb1", size=100),
                    dict(TRADE, timestamp=now + 2, asset="A", transactionHash="0xb2", size=100)]
    bot.poll()
    pos = bot.state["positions"]
    assert pos["A"]["who"] == ["alice", "bob"] and pos["B"]["who"] == ["bob"]
    assert {t["who"] for t in bot.state["trades"]} == {"alice", "bob"}
    assert bot.state["traders"]["0xb"]["buys"] == 2
    # alice sells all of hers: that is half of the stack we copied into A, so we sell half.
    shares = pos["A"]["shares"]
    feeds["0xa"].append(dict(TRADE, timestamp=now + 3, asset="A", transactionHash="0xa2", side="SELL", size=100))
    # a sell of B by alice is ignored: we never copied B from her.
    feeds["0xa"].append(dict(TRADE, timestamp=now + 4, asset="B", transactionHash="0xa3", side="SELL", size=100))
    bot.poll()
    assert abs(pos["A"]["shares"] - shares / 2) < 1e-9 and "B" in pos
    assert bot.state["trades"][0]["side"] == "SELL" and bot.state["trades"][0]["who"] == "alice, bob"
    # Removing a trader stops following him; his open positions stay.
    store.write("settings.json", {**bot.settings, "target": "0xb", "target_label": "bob"}, "")
    bot.refresh_settings(); bot.poll()
    assert list(bot.state["cursors"]) == ["0xb"] and "B" in pos


def test_order_fill_parsing():
    resp = {"executions": [{"lastShares": "10", "lastPx": {"value": "0.40"}},
                           {"lastShares": "5", "lastPx": {"value": "0.43"}}]}
    shares, px, fee = live_us.filled(resp)
    assert shares == 15 and abs(px - 0.41) < 1e-9 and fee is None
    resp["executions"][0]["fee"] = {"value": "0.17"}
    assert live_us.filled(resp)[2] == 0.17          # exchange-reported fee wins when present


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


def test_exposure_and_drawdown_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "paper_start_bank": 10, "size_mode": "2",
                                  "max_exposure_pct": 50}, "")
    bot = main.Bot(store)
    for i in range(5):
        bot.copy(dict(TRADE, asset=f"A{i}", conditionId=f"m{i}"), 1)
    assert len(bot.state["positions"]) == 2 and bot.state["skipped"]["exposure limit"] == 3
    for p in bot.state["positions"].values():
        p["cur"] = 0.01                                  # both bets collapse
    bot.copy(dict(TRADE, asset="Z", conditionId="mz"), 1)
    assert bot.settings["halted"] is True and store.read("settings.json")[0]["halted"] is True


def test_per_bet_cap_and_cash_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "paper_start_bank": 300, "size_mode": "5",
                                  "max_exposure_pct": 100, "max_per_bet": 50, "min_cash": 50}, "")
    bot = main.Bot(store)
    for _ in range(12):                                  # he buys the same outcome 12 times
        bot.copy(dict(TRADE), 1)
    assert round(bot.state["positions"]["A"]["gross_in"], 2) == 50.0
    assert bot.state["skipped"]["already $50 in this bet"] == 2
    for i in range(60):                                  # then lots of other bets
        bot.copy(dict(TRADE, asset=f"B{i}", conditionId=f"m{i}"), 1)
    assert bot.state["bank"] >= 50 and bot.state["skipped"]["would take cash under $50"] > 0


def test_blocked_league_is_never_bought(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "size_mode": "5", "block_leagues": "MLS, epl"}, "")
    bot = main.Bot(store)
    bot.copy(dict(TRADE, asset="M", slug="mls-ner-chi-2026-10-17-ner", eventSlug="mls-ner-chi-2026-10-17"), 1)
    assert "M" not in bot.state["positions"] and bot.state["skipped"]["MLS blocked"] == 1
    bot.copy(dict(TRADE), 1)                             # other leagues still copy
    assert "A" in bot.state["positions"]


def test_per_bet_cap_is_shared_across_traders(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.38, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "paper_start_bank": 300, "size_mode": "5", "target": "0xa,0xb",
                                  "target_label": "ic4cream, juice-fruit", "max_exposure_pct": 100, "max_per_bet": 50}, "")
    bot = main.Bot(store)
    for _ in range(10):                                  # ic4cream fills the Broncos bet to $50
        bot.copy(dict(TRADE, _wallet="0xa", _who="ic4cream"), 1)
    bot.copy(dict(TRADE, _wallet="0xb", _who="juice-fruit"), 1)   # juice-fruit buys the same side
    pos = bot.state["positions"]["A"]
    assert round(pos["gross_in"], 2) == 50.0 and pos["who"] == ["ic4cream"]
    assert bot.state["skipped"]["already $50 in this bet"] == 1


def test_slippage_cap_skips_buys_priced_past_his(tmp_path, monkeypatch):
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    asks = {"atc-a": 0.405, "atc-b": 0.43}
    monkeypatch.setattr(pm, "best_prices", lambda key: (0.38, asks.get(key, 0.5)))
    bot = main.Bot(LocalStore(str(tmp_path)))
    bot.settings["max_slip_cents"] = 1.0
    bot.copy(dict(TRADE, asset="A", slug="a"), 2)    # 0.5c above his 0.40: copied
    bot.copy(dict(TRADE, asset="B", slug="b"), 2)    # 3c above: skipped
    assert list(bot.state["positions"]) == ["A"]
    assert bot.state["skipped"] == {"price moved past slippage cap": 1}
    assert bot.state["slip"]["n"] == 1 and bot.state["slip"]["skipped"] == 1
    assert abs(bot.state["slip"]["cents"] - 0.5) < 1e-6
    assert bot.state["positions"]["A"]["us_slug"] == "atc-a"


def test_us_side_matching(monkeypatch):
    monkeypatch.setattr(live_us, "_sides", lambda slug: ((("eagles", "philadelphia eagles"), True),
                                                       (("packers", "green bay packers"), False)))
    assert live_us.is_long("aec-x", "Packers", 0) is False
    assert live_us.is_long("aec-x", "Eagles", 1) is True
    assert live_us.is_long("aec-x", "No", 0) is False
    assert live_us.is_long("aec-x", "Yes", 1) is True


def test_exits_follow_his_share_of_stack_even_when_halted(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "best_prices", lambda a: (0.40, 0.42))
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    monkeypatch.setattr(pm, "his_position_shares", lambda w, a: 400.0)  # he held 400 before we started
    bot = main.Bot(LocalStore(str(tmp_path)))
    bot.copy(dict(TRADE, size=100), 1)             # we start copying: he now has 400 (API) incl. this buy
    bot.settings["max_slip_cents"] = 0
    bot.copy(dict(TRADE, size=200), 1)             # skipped for price, but he now has 600
    assert bot.state["positions"]["A"]["his_shares"] == 600
    ours = bot.state["positions"]["A"]["shares"]
    bot.settings["halted"] = True
    bot.copy(dict(TRADE, side="SELL", size=300), 1)  # half his stack
    assert abs(bot.state["positions"]["A"]["shares"] - ours / 2) < 1e-9
    bot.copy(dict(TRADE, side="SELL", size=300), 1)  # the rest: we're out too
    assert "A" not in bot.state["positions"]


def test_market_matching_rejects_ambiguous_and_mismatched(monkeypatch):
    monkeypatch.setattr(live_us, "_us_event", lambda ev: ("aec-mlb-lad-mil-2026-10-12",))
    teams = ((("los angeles dodgers",), True), (("milwaukee brewers",), False))
    monkeypatch.setattr(live_us, "_market", lambda slug: (teams, 0.0695, False))
    assert live_us.match("mlb-lad-mil-2026-10-12", "mlb-lad-mil-2026-10-12", "Milwaukee Brewers") == \
        ("aec-mlb-lad-mil-2026-10-12", False, "")
    # An outcome that names neither side, or both, is refused rather than guessed.
    assert live_us.match("mlb-lad-mil-2026-10-12", "mlb-lad-mil-2026-10-12", "Over 8.5")[2] == \
        "outcome doesn't match a US side"
    monkeypatch.setattr(live_us, "_market", lambda slug: (((("brewers",), True), (("brewers b",), False)), 0.0695, False))
    assert live_us.side_for("x", "brewers b") is False  # exact name wins over a looser match
    monkeypatch.setattr(live_us, "_market", lambda slug: (((("milwaukee",), True), (("milwaukee b",), False)), 0.0695, False))
    assert live_us.side_for("x", "milwaukee brewers") is None
    # Two US contracts ending in the same slug: ambiguous.
    monkeypatch.setattr(live_us, "_us_event", lambda ev: ("aec-x-1", "cpic-x-1"))
    assert live_us.us_market("x-1", "x") is None
    monkeypatch.setattr(live_us, "_us_event", lambda ev: ("aec-x-1",))
    monkeypatch.setattr(live_us, "_market", lambda slug: (teams, 0.0695, True))
    assert live_us.match("x-1", "x", "Yes")[2] == "US market closed"


def _bot(tmp_path, monkeypatch, ask=0.42, depth=1e4, bid=0.40):
    monkeypatch.setattr(live_us, "us_market", lambda slug, event="": "atc-" + slug)
    monkeypatch.setattr(pm, "best_prices", lambda a: (bid, ask))
    monkeypatch.setattr(live_us, "book", lambda slug, long: {"bid": bid, "ask": ask, "bid_shares": depth,
                                                             "ask_shares": depth, "spread": round(ask - bid, 4)})
    return main.Bot(LocalStore(str(tmp_path)))


def test_copy_filters_explain_skips(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch, ask=0.42, bid=0.30)       # 12c spread
    bot.copy(dict(TRADE), 5)
    bot.settings["max_spread_cents"] = 20
    bot.copy(dict(TRADE, transactionHash="0x2"), 300)            # five minutes late
    bot.settings["min_depth_usd"] = 10_000
    bot.copy(dict(TRADE, transactionHash="0x3"), 5)
    assert bot.state["skipped"] == {"spread too wide": 1, "copy too late (over 60s)": 1, "too little liquidity": 1}
    assert not bot.state["positions"]
    # The shadow book took all three at the ask, so the dashboard can compare.
    assert len(bot.state["shadow"]["positions"]) == 1 and bot.state["shadow"]["totals"]["gross_in"] == 3


def test_thin_book_gives_partial_fill(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch, depth=60)                 # 60 shares at 42c = $25.20 on offer
    bot.settings.update(size_mode="5", min_depth_usd=0)
    bot.copy(dict(TRADE), 2)
    bot.settings["size_mode"] = "prop"; bot.settings["prop_pct"] = 10   # $40 wanted
    bot.copy(dict(TRADE, transactionHash="0x2"), 2)
    pos = bot.state["positions"]["A"]
    assert abs(pos["gross_in"] - (5 + 60 * 0.42)) < 1e-9 and pos["partial"] == 1
    assert bot.state["trades"][0]["fill"] == "partial" and bot.state["trades"][0]["dst"] == "atc-will-x"
    assert bot.state["trades"][0]["src"] == "A" and bot.state["trades"][0]["ask"] == 0.42


def test_closed_ledger_splits_gross_fees_and_slippage(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch, ask=0.42, bid=0.41)
    bot.copy(dict(TRADE), 4)                                     # $1 at 42c, he paid 40c
    bot.copy(dict(TRADE, side="SELL", size=1000, transactionHash="0x2"), 4)   # he sells everything
    row = bot.state["closed"][0]
    shares = 1 / 0.42
    assert row["fees_in"] == taker_fee(shares, 0.42) and row["fees_out"] == taker_fee(shares, 0.41)
    assert abs(row["slip"] - shares * 0.02) < 1e-4 and row["exit"] == "copied sell"
    assert abs(row["gross"] - (shares * 0.41 - 1)) < 1e-4
    assert abs(row["net"] - (row["gross"] - row["fees_in"] - row["fees_out"])) < 1e-4
    t = bot.state["totals"]
    assert abs(t["gross_out"] - t["gross_in"] - t["fees_in"] - t["fees_out"] - bot.state["realized"]) < 1e-6


def test_partial_exits_and_settlement(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch, ask=0.40, bid=0.40)
    bot.copy(dict(TRADE, size=1000), 1)
    bot.copy(dict(TRADE, side="SELL", size=250, transactionHash="0x2"), 1)   # a quarter of his 1000
    shares = 1 / 0.40
    assert abs(bot.state["positions"]["A"]["shares"] - shares * 0.75) < 1e-9 and not bot.state["closed"]
    monkeypatch.setattr(pm, "resolution", lambda a: 1.0)
    monkeypatch.setattr(pm, "leaderboard_pnl", lambda w: {})
    bot.upkeep()
    row = bot.state["closed"][0]
    assert row["exit"] == "resolved" and bot.state["totals"]["settled"] > 0
    assert bot.state["shadow"]["wins"] == 1 and not bot.state["shadow"]["positions"]


def test_settles_when_polymarket_us_resolves_first(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch, ask=0.46, bid=0.45)
    bot.copy(dict(TRADE, outcome="Yes", price=0.45), 3)
    assert bot.state["positions"]
    monkeypatch.setattr(pm, "resolution", lambda a: None)          # international still pending
    monkeypatch.setattr(pm, "midpoint", lambda a: 0.9995)
    monkeypatch.setattr(pm, "leaderboard_pnl", lambda w: {})
    monkeypatch.setattr(live_us, "settlement", lambda slug, long: 1.0 if long else 0.0)
    bot.upkeep()
    assert not bot.state["positions"] and bot.state["wins"] == 1 and bot.state["realized"] > 0
    assert bot.state["closed"][0]["exit"] == "resolved"


def test_older_position_without_side_settles_on_its_own_side(tmp_path, monkeypatch):
    # A Brewers position bought before sides were stored: Brewers are the short side
    # of the US contract, and the Brewers won. It must settle as a win, not a loss.
    bot = _bot(tmp_path, monkeypatch, ask=0.46, bid=0.45)
    teams = ((("los angeles dodgers",), True), (("milwaukee brewers",), False))
    monkeypatch.setattr(live_us, "_market", lambda slug: (teams, 0.0695, False))
    bot.copy(dict(TRADE, outcome="Milwaukee Brewers", price=0.45), 3)
    pos = bot.state["positions"]["A"]
    assert pos.pop("long") is False
    monkeypatch.setattr(live_us, "settlement", lambda slug, long: 0.0 if long else 1.0)
    monkeypatch.setattr(pm, "resolution", lambda a: None)
    monkeypatch.setattr(pm, "midpoint", lambda a: 0.9995)
    monkeypatch.setattr(pm, "leaderboard_pnl", lambda w: {})
    bot.upkeep()
    assert bot.state["wins"] == 1 and bot.state["realized"] > 0


def test_live_switch_falls_back_to_paper_with_a_reason(tmp_path, monkeypatch):
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "mode": "live"}, "")
    monkeypatch.setattr(live_us, "has_keys", lambda: True)
    monkeypatch.setattr(live_us, "account", lambda: None)              # keys rejected
    bot = main.Bot(store)
    assert bot.mode == "paper" and "didn't accept" in bot.live_error
    monkeypatch.setattr(live_us, "account", lambda: {"cash": 250.0, "buying_power": 240.0,
                                                     "positions_value": 10.0, "open_orders": 0.0})
    monkeypatch.setattr(live_us, "positions", lambda: {})
    bot2 = main.Bot(LocalStore(str(tmp_path)))
    assert bot2.mode == "live" and bot2.state["bank"] == 240.0 and bot2.state["start_bank"] == 250.0


def test_live_sync_takes_cash_and_shares_from_the_account(tmp_path, monkeypatch):
    store = LocalStore(str(tmp_path))
    store.write("settings.json", {**DEFAULT_SETTINGS, "mode": "live"}, "")
    monkeypatch.setattr(live_us, "has_keys", lambda: True)
    acct = {"cash": 100.0, "buying_power": 100.0, "positions_value": 0.0, "open_orders": 0.0}
    monkeypatch.setattr(live_us, "account", lambda: dict(acct))
    bot = _bot(tmp_path, monkeypatch)
    bot.store, bot.settings = store, bot.load_settings()
    bot.mode = bot.effective_mode()
    bot.state = bot.load_state()
    st = bot.state
    buy(st, dict(TRADE, asset="A"), 5, 0.40, 1)
    st["positions"]["A"].update(us_slug="aec-a", long=True)
    buy(st, dict(TRADE, asset="B", title="Other"), 5, 0.40, 1)
    st["positions"]["B"].update(us_slug="aec-b", long=True)
    acct["buying_power"] = 300.0                                      # he deposited $200
    monkeypatch.setattr(live_us, "positions", lambda: {
        "aec-a": {"qty": 10.0, "cost": 4, "realized": 0, "value": 0, "outcome": "", "title": "", "expired": False},
        "aec-x": {"qty": 3.0, "cost": 1, "realized": 0, "value": 0, "outcome": "Yes", "title": "Manual bet", "expired": False}})
    bot.sync_account()
    assert st["bank"] == 300.0
    assert st["positions"]["A"]["shares"] == 10.0                      # account had fewer than we thought
    assert "B" not in st["positions"]                                  # sold outside the bot
    assert [o["slug"] for o in st["account"]["outside"]] == ["aec-x"]
    assert st["start_bank"] > 250 and "Deposit" in st["log"][0][1]    # a deposit isn't profit


def test_orders_round_to_tick_in_our_favour(monkeypatch):
    monkeypatch.setattr(live_us, "_market_raw", lambda slug: {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 1})
    assert live_us._round_order("m", "BUY", 2.7, 0.4689) == (2.0, 0.46)
    assert live_us._round_order("m", "SELL", 2.0, 0.4611) == (2.0, 0.47)
    resp = {"executions": [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "INSUFFICIENT_FUNDS"}]}
    assert live_us.rejection(resp) == "INSUFFICIENT_FUNDS" and live_us.filled(resp)[0] == 0
    fill = {"executions": [{"lastShares": "2", "lastPx": {"value": "0.46"},
                            "commissionNotionalCollected": {"value": "0.03", "currency": "USD"}}]}
    assert live_us.filled(fill)[2] == 0.03
    # NO orders are priced in YES terms both ways: buy NO at <= 0.46 is sent as 0.54,
    # and a fill reported at 0.56 means we paid 0.44 for NO.
    sent = []
    class C:
        orders = type("O", (), {"create": staticmethod(lambda o: sent.append(o) or {})})
        def close(self): pass
    import pytest as _p
    mp = _p.MonkeyPatch(); mp.setattr(live_us, "_client", lambda: C())
    live_us.place("m", False, "BUY", 10, 0.46)
    mp.undo()
    assert sent[0]["price"]["value"] == "0.5400" and sent[0]["outcomeSide"] == "OUTCOME_SIDE_NO"
    no_fill = {"executions": [{"lastShares": "10.86", "lastPx": {"value": "0.56"}}]}
    assert abs(live_us.filled(no_fill, long=False)[1] - 0.44) < 1e-9
