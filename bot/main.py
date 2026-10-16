"""The copy loop: watch the target wallet and mirror each trade.

    python -m bot.main                 # GitHub Actions (uses GITHUB_TOKEN + repo)
    python -m bot.main --local         # on a computer, state in ./local-data

Runs for RUN_SECONDS, then exits so the workflow can start a fresh run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Optional

from . import live_us, polymarket as pm
from .portfolio import (DEFAULT_SETTINGS, add_total, bet_size, buy, exposure, log, mark, new_state,
                        sell, should_copy_buy, skip, slip_ok, taker_fee, track_slip, TAKER_FEE)
from .store import LocalStore, Store

POLL = float(os.environ.get("POLL_SECONDS", "3"))
SAVE_EVERY = int(os.environ.get("SAVE_SECONDS", "15"))         # publish changes this often
UPKEEP_EVERY = int(os.environ.get("UPKEEP_SECONDS", "120"))    # re-price open positions
HEARTBEAT_EVERY = int(os.environ.get("HEARTBEAT_SECONDS", "300"))
UPKEEP_BATCH = int(os.environ.get("UPKEEP_BATCH", "150"))
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "1500"))
RUN_SECONDS = int(float(os.environ.get("RUN_MINUTES") or 340) * 60)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
L = logging.getLogger("bot")


class Bot:
    def __init__(self, store):
        self.store = store
        self.store.ensure_branch()
        self.settings = self.load_settings()
        self.mode = self.effective_mode()
        self.state = self.load_state()
        self.dirty = True

    # ------------------------------------------------------------ settings --
    def load_settings(self) -> dict:
        s, _ = self.store.read("settings.json")
        if s is None:
            s = dict(DEFAULT_SETTINGS)
            self.store.write("settings.json", s, "Default settings")
        return {**DEFAULT_SETTINGS, **s}

    def effective_mode(self) -> str:
        """Live only when asked for AND the keys reach a real account; otherwise paper, with the reason saved."""
        self.live_error = ""
        if self.settings.get("mode") == "live":
            if getattr(self, "mode", None) == "live" and live_us.has_keys():
                return "live"  # already checked the account when we switched
            if not live_us.has_keys():
                self.live_error = "No Polymarket US keys saved yet (GO LIVE tab)."
            elif live_us.account() is None:
                self.live_error = "Polymarket US didn't accept the keys, or couldn't be reached. Still on paper."
            else:
                return "live"
            L.warning("Live mode requested but unavailable: %s", self.live_error)
        return "paper"

    def state_path(self) -> str:
        return f"state-{self.mode}.json"

    def load_state(self) -> dict:
        st, _ = self.store.read(self.state_path())
        if st is None:
            bank = float(self.settings["paper_start_bank"])
            if self.mode == "live":
                acct = live_us.account() or {}
                bank = acct.get("buying_power", 0.0)
            st = new_state(bank)
            if self.mode == "live":
                # P&L is measured from the whole account as it was at go-live, cash plus anything held.
                st["start_bank"] = round(bank + acct.get("positions_value", 0.0), 2)
                st["account"] = {**acct, "synced_at": int(time.time())}
        if "fees" not in st:
            # Positions opened before fees were tracked: charge the buy fee they would have paid.
            st["fees"] = 0.0
            for pos in st["positions"].values():
                fee = taker_fee(pos["shares"], pos["avg"])
                pos.update(cost=pos["cost"] + fee, theta=pos.get("theta", TAKER_FEE))
                pos["avg"] = pos["cost"] / pos["shares"]
                st["bank"] -= fee
                st["fees"] = round(st["fees"] + fee, 2)
            log(st, f"Charged ${st['fees']:.2f} of fees on positions opened before fees were tracked")
        if "totals" not in st:
            # Positions opened before the cost breakdown existed: rebuild it from the fill log.
            st.setdefault("closed", [])
            for pos in st["positions"].values():
                buys = [r for r in st["trades"] if r["side"] == "BUY" and r["title"] == pos["title"]
                        and r["outcome"] == pos["outcome"]]
                fee = round(pos["cost"] - sum(r["usd"] for r in buys), 2) if buys else 0.0
                pos.update(gross_in=pos["cost"] - fee, fees_in=fee, fills=len(buys) or 1,
                           delay_sum=sum(r.get("delay") or 0 for r in buys),
                           slip=sum(r["shares"] * (r["price"] - float(r.get("his_price") or r["price"])) for r in buys),
                           category=pos.get("category") or (pos.get("slug") or "other").split("-")[0])
                add_total(st, gross_in=pos["gross_in"], fees_in=fee, slip=pos["slip"])
        if not st.get("who_backfilled"):
            self.backfill_who(st)
        if self.mode == "live" and not st.get("no_px_fixed"):
            self.fix_no_fills(st)
        st["mode"] = self.mode
        return st

    @staticmethod
    def backfill_who(st: dict) -> None:
        """One-time: tag fills and positions from before trader tags existed with whoever the
        bot was copying at the time, read from the 'Now copying X' log lines."""
        switches = sorted((ts, m[len("Now copying "):]) for ts, m in st.get("log", []) if m.startswith("Now copying "))
        def at(ts):
            who = None
            for t, name in switches:
                if t <= ts:
                    who = name
            return who
        for row in st.get("trades", []):
            if "who" not in row and row.get("side") == "BUY" and at(row.get("ts", 0)):
                row["who"] = at(row["ts"])
        for pos in st.get("positions", {}).values():
            if not pos.get("who"):
                names = {r["who"] for r in st["trades"] if r.get("side") == "BUY" and r.get("who")
                         and r["title"] == pos["title"] and r["outcome"] == pos["outcome"]}
                if names:
                    pos["who"] = sorted(names)
        for row in st.get("trades", []):
            if "who" not in row and row.get("side") != "BUY":
                names = {r["who"] for r in st["trades"] if r.get("side") == "BUY" and r.get("who")
                         and r["title"] == row["title"] and r["outcome"] == row["outcome"]}
                if names:
                    row["who"] = ", ".join(sorted(names))
        for row in st.get("closed", []):
            if not row.get("who"):
                names = {r["who"] for r in st["trades"] if r.get("side") == "BUY" and r.get("who")
                         and r["title"] == row["title"] and r["outcome"] == row["outcome"]}
                row["who"] = ", ".join(sorted(names))
        st["who_backfilled"] = True

    @staticmethod
    def fix_no_fills(st: dict) -> None:
        """One-time repair: NO buys filled before the YES-terms fix were booked at the YES
        price (paid 0.44, booked 0.56). Rebook them at what was paid, and undo the
        'deposit' the account sync then saw in the leftover cash."""
        for row in st["trades"]:
            if row.get("side") != "BUY" or row.get("dst_side") != "NO" or not row.get("fee_source"):
                continue
            paid = round(1 - row["price"], 4)
            diff = row["shares"] * (row["price"] - paid)
            row.update(price=paid, usd=round(row["usd"] - diff, 2))
            for pos in st["positions"].values():
                if pos["title"] == row["title"] and pos["outcome"] == row["outcome"] and pos.get("long") is False:
                    for k in ("cost", "gross_in", "slip"):
                        pos[k] = pos.get(k, 0.0) - diff
                    pos["avg"] = pos["cost"] / pos["shares"]
            add_total(st, gross_in=-diff, slip=-diff)
            for ts, msg in st["log"]:
                if msg.startswith("Deposit of $") and 0 <= ts - row["ts"] < 1800 \
                        and abs(float(msg.split("$")[1].split()[0]) - diff) < 0.05:
                    st["start_bank"] = round(st["start_bank"] - float(msg.split("$")[1].split()[0]), 2)
            log(st, f"Rebooked NO buy of {row['title']} at the {paid * 100:.0f}c actually paid")
        st["no_px_fixed"] = True

    def save(self, why: str = "Update state") -> None:
        self.state["heartbeat"] = int(time.time())
        self.state["live_error"] = getattr(self, "live_error", "")
        self.state["settings_seen"] = self.settings
        mark(self.state)
        self.store.write(self.state_path(), self.state, why)
        self.dirty = False

    def refresh_settings(self) -> None:
        s = self.load_settings()
        old_mode = self.mode
        self.settings = s
        self.mode = self.effective_mode()
        if self.mode != old_mode:
            self.save(f"Leave {old_mode} mode")
            self.state = self.load_state()
            log(self.state, f"Switched to {self.mode.upper()} mode")
            self.dirty = True
        if s.get("reset_paper") and self.mode == "paper":
            self.state = new_state(float(s["paper_start_bank"]))
            self.state["mode"] = "paper"
            log(self.state, "Paper account reset")
            s["reset_paper"] = False
            self.store.write("settings.json", s, "Clear reset flag")
            self.dirty = True
        if s.get("kill"):
            self.kill_switch()
            s["kill"] = False
            s["halted"] = True
            self.store.write("settings.json", s, "Kill switch done; halted")
            self.settings = s

    # ---------------------------------------------------------------- copy --
    def targets(self) -> list:
        """[(wallet, label)] for every trader we copy. settings.target is a comma list of
        wallets and target_label the matching comma list of names."""
        wallets = [w.strip().lower() for w in str(self.settings.get("target", "")).split(",") if w.strip()]
        labels = [x.strip() for x in str(self.settings.get("target_label") or "").split(",")]
        return [(w, labels[i] if i < len(labels) and labels[i] else w[:8]) for i, w in enumerate(wallets)]

    def target(self) -> str:
        """The first trader's wallet (used where a single wallet is needed and none was given)."""
        ts = self.targets()
        return ts[0][0] if ts else ""

    def sync_traders(self) -> None:
        """Start a cursor for each newly added trader (from now on, no replay) and drop removed ones."""
        st = self.state
        curs, traders = st.setdefault("cursors", {}), st.setdefault("traders", {})
        old = st.pop("cursor", None)  # single-trader state from before
        if old and old.get("target"):
            curs.setdefault(old["target"], {"ts": old.get("ts", 0), "seen": old.get("seen", [])})
            his = st.pop("his", None) or {}
            traders.setdefault(old["target"], {"since": int(time.time()), "buys": 0, "sells": 0, "usd": 0.0, **his})
        wanted = dict(self.targets())
        for w, label in wanted.items():
            if w not in curs:
                curs[w] = {"ts": int(time.time()), "seen": []}
                traders[w] = {"since": int(time.time()), "buys": 0, "sells": 0, "usd": 0.0}
                log(st, f"Now copying {label}")
                self.dirty = True
            traders.setdefault(w, {"since": int(time.time()), "buys": 0, "sells": 0, "usd": 0.0})["label"] = label
        for w in [w for w in curs if w not in wanted]:
            log(st, f"Stopped copying {traders.get(w, {}).get('label') or w[:8]}")
            curs.pop(w)
            traders.pop(w, None)
            self.dirty = True

    def poll(self) -> None:
        self.sync_traders()
        for wallet, label in self.targets():
            self.poll_one(wallet, label)

    def poll_one(self, wallet: str, label: str) -> None:
        cur = self.state["cursors"][wallet]
        trades = pm.new_trades(wallet, cur["ts"])
        if trades:
            L.info("%d trade rows from %s since %d", len(trades), label, cur["ts"])
        seen = set(cur["seen"])
        for t in trades:
            k = pm.trade_key(t)
            if k in seen:
                continue
            seen.add(k)
            ts = int(t.get("timestamp") or 0)
            if ts > cur["ts"]:
                cur["ts"] = ts
                seen = {k}
            his = self.state["traders"][wallet]
            his["buys" if str(t.get("side")).upper() == "BUY" else "sells"] += 1
            his["usd"] = round(his["usd"] + float(t.get("usdcSize") or 0), 2)
            self.copy({**t, "_wallet": wallet, "_who": label}, max(0, int(time.time()) - ts))
            self.dirty = True
        cur["seen"] = list(seen)[-200:]

    def copy(self, t: dict, delay: int) -> None:
        side = str(t.get("side")).upper()
        wallet = t.get("_wallet") or self.target()
        who = {"who": t.get("_who") or (dict(self.targets()).get(wallet) or wallet[:8]), "wallet": wallet}
        asset = str(t.get("asset"))
        his_usd = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * float(t.get("price") or 0)
        us_slug, long, no_match = live_us.match(t.get("slug") or "", t.get("eventSlug") or "", t.get("outcome"))
        on_us = us_slug is not None
        his_size = float(t.get("size") or 0)
        if side == "BUY":
            self.state["us"]["checked"] += 1
            self.state["us"]["available"] += on_us
            held = self.state["positions"].get(asset)
            if held is not None and "his_shares" in held and wallet in held.get("seeded", held.get("wallets", [wallet])):
                held["his_shares"] += his_size  # so we know what share of his stack a later sell is
        if side == "BUY":
            why = should_copy_buy(self.settings, his_usd)
            if why:
                return skip(self.state, why)
            if self.settings.get("us_only", True) and not on_us:
                return skip(self.state, no_match)
            if not on_us:
                long = True  # international book, outcome token is exact
            if self.settings.get("once_per_market", False) and asset in self.state["positions"]:
                return skip(self.state, "already holding")
            cond = t.get("conditionId")
            if self.settings.get("skip_both_sides", False) and cond and any(
                    p.get("condition_id") == cond and a != asset for a, p in self.state["positions"].items()):
                return skip(self.state, "other side of a market we hold")
            if asset not in self.state["positions"] and len(self.state["positions"]) >= MAX_POSITIONS:
                return skip(self.state, "too many open positions")
            usd = bet_size(self.settings, his_usd)
            equity = self.state["bank"] + exposure(self.state)
            if equity < self.state["start_bank"] * (1 - float(self.settings.get("max_drawdown_pct", 25)) / 100):
                self.halt(f"Account down {self.settings.get('max_drawdown_pct', 25)}% from start; halted")
                return skip(self.state, "drawdown limit")
            if exposure(self.state) + usd > equity * float(self.settings.get("max_exposure_pct", 50)) / 100:
                return skip(self.state, "exposure limit")
            cap = float(self.settings.get("max_per_bet", 0) or 0)
            if cap and held is not None:
                usd = min(usd, cap - held.get("gross_in", held["cost"]))
                if usd < 0.5:
                    return skip(self.state, f"already ${cap:.0f} in this bet")
            floor = float(self.settings.get("min_cash", 0) or 0)
            if floor and self.state["bank"] - usd * 1.05 < floor:
                return skip(self.state, f"would take cash under ${floor:.0f}")
            his_price = float(t.get("price") or 0)
            # Price off the book we'd really trade on: Polymarket US when the market is there.
            if us_slug:
                b = live_us.book(us_slug, long) or {}
            else:
                bid, ask = pm.best_prices(asset)
                b = {"bid": bid, "ask": ask, "spread": round(ask - bid, 4) if bid and ask else None}
            ask = b.get("ask")
            if ask is None:
                return skip(self.state, "no sellers")
            self.shadow_buy(t, usd, ask, delay, us_slug, long, who)
            why = self.price_filter(b, his_price, delay)
            if why:
                return skip(self.state, why)
            meta = {"src": asset, "src_market": t.get("conditionId"), "dst": us_slug, "dst_side": "YES" if long else "NO",
                    "bid": b.get("bid"), "ask": ask, "spread": b.get("spread"), "depth": b.get("ask_shares"), **who}
            if self.mode == "live":
                return self.live_buy(t, usd, us_slug, long, ask, delay, meta)
            # Paper fill: we can only take what's resting at the best ask; the rest is a missed fill.
            depth = b.get("ask_shares") or 0
            partial = bool(depth) and usd / ask > depth
            if partial:
                usd = depth * ask
                skip(self.state, "partial fill (book too thin)")
            meta["fill"] = "partial" if partial else "full"
            fill = buy(self.state, t, usd, ask, delay, live_us.fee_coefficient(us_slug), meta=meta)
            if fill:
                track_slip(self.state, his_price, ask)
                pos = self.state["positions"][asset]
                pos.update(us=on_us, us_slug=us_slug, long=long)
                pos["partial"] = pos.get("partial", 0) + partial
                self.seed_his_shares(pos, his_size, wallet)
                L.info("BUY  $%.2f %s %s @ %.3f (he paid %.3f, %ds late)", usd, t.get("outcome"),
                       t.get("title"), ask, his_price, delay)
        elif side == "SELL":
            # Exits are followed even while halted: halting stops new bets, not getting out.
            if not self.settings.get("copy_sells"):
                return
            sold = float(t.get("size") or 0)
            self.shadow_sell(asset, sold, wallet)
            pos = self.state["positions"].get(asset)
            if pos is None or wallet not in pos.get("wallets", [wallet]):
                return  # not a position we copied from this trader
            fraction = self.exit_fraction(pos, asset, sold, wallet)
            if self.mode == "live":
                return self.live_sell(asset, fraction)
            bid, _ = self.quote(asset)
            if bid is None:
                return skip(self.state, "no buyers")
            sell(self.state, asset, fraction, bid, "copied sell")
            L.info("SELL %.0f%% of %s @ %.3f", fraction * 100, t.get("title"), bid)

    def price_filter(self, b: dict, his_price: float, delay: int) -> Optional[str]:
        """Why this copy is too late, too thin, too wide or too expensive to take; None if it's fine."""
        st = self.settings
        if delay > float(st.get("max_delay_s", 60)):
            return f"copy too late (over {int(float(st.get('max_delay_s', 60)))}s)"
        if b.get("spread") is not None and b["spread"] * 100 > float(st.get("max_spread_cents", 5)) + 1e-9:
            return "spread too wide"
        if "ask_shares" in b and b["ask_shares"] * b["ask"] < float(st.get("min_depth_usd", 0)):
            return "too little liquidity"
        if not slip_ok(st, his_price, b["ask"]):
            self.state.setdefault("slip", {"n": 0, "cents": 0.0, "skipped": 0})["skipped"] += 1
            return "price moved past slippage cap"
        return None

    def exit_fraction(self, pos: dict, asset: str, sold: float, wallet: str) -> float:
        """What fraction of a position to sell when a trader sells `sold` shares: that sale's share of
        the stack held by the traders we copied into it."""
        before = pos.get("his_shares")
        if not before:
            before = sold + pm.his_position_shares(wallet, asset)
        fraction = min(1.0, sold / before) if before > 0 else 1.0
        pos["his_shares"] = max(0.0, before - sold)
        return 1.0 if pos["his_shares"] < 1e-6 else fraction  # he's fully out, so are we

    # Shadow book: the same signals copied immediately at whatever the ask is, with no
    # delay/spread/liquidity/price filters, so the dashboard can compare the two.
    def shadow(self) -> dict:
        sh = self.state.get("shadow")
        if not sh:
            sh = self.state["shadow"] = new_state(float(self.state["start_bank"]))
            for k in ("trades", "equity", "log", "cursor", "cursors", "traders", "us", "live_spent_today"):
                sh.pop(k, None)
            # Start from where the real book is, so the two only differ by what the filters skip.
            for k in ("bank", "realized", "wins", "losses", "fees", "positions", "totals"):
                sh[k] = json.loads(json.dumps(self.state.get(k, sh.get(k))))
        sh.setdefault("trades", [])
        return sh

    def _shadow_tidy(self, sh: dict) -> None:
        sh["trades"] = []
        del sh.setdefault("closed", [])[:]

    def shadow_buy(self, t: dict, usd: float, ask: float, delay: int, us_slug, long, who: dict) -> None:
        sh = self.shadow()
        asset = str(t["asset"])
        size = float(t.get("size") or 0)
        held = sh["positions"].get(asset)
        if held is not None:
            held["his_shares"] = held.get("his_shares", 0.0) + size
        if buy(sh, t, usd, ask, delay, live_us.fee_coefficient(us_slug), meta=who) and held is None:
            pos = sh["positions"][asset]
            pos.update(us_slug=us_slug, long=long)
            pos["his_shares"] = max(pm.his_position_shares(who["wallet"], asset), size)
        self._shadow_tidy(sh)

    def shadow_sell(self, asset: str, sold: float, wallet: str) -> None:
        sh = self.shadow()
        pos = sh["positions"].get(asset)
        if not pos or wallet not in pos.get("wallets", [wallet]):
            return
        fraction = self.exit_fraction(pos, asset, sold, wallet)
        bid, _ = live_us.quote(pos["us_slug"], pos["long"]) if pos.get("us_slug") and self.us_side(pos) is not None \
            else pm.best_prices(asset)
        if bid is not None:
            sell(sh, asset, fraction, bid, "copied sell")
        self._shadow_tidy(sh)

    def seed_his_shares(self, pos: dict, his_size: float, wallet: str) -> None:
        """On a trader's first copied buy of an outcome, add the shares he holds (including any from
        before we started) to the stack our exits are measured against."""
        seeded = pos.setdefault("seeded", pos.get("wallets", [wallet])[:1] if "his_shares" in pos else [])
        if wallet not in seeded:
            pos["his_shares"] = pos.get("his_shares", 0.0) + max(pm.his_position_shares(wallet, pos["asset"]), his_size)
            seeded.append(wallet)

    # ---------------------------------------------------------------- live --
    def quote(self, asset: str):
        """(bid, ask) for a position we hold, from the book it trades on."""
        pos = self.state["positions"].get(asset) or {}
        if pos.get("us_slug") and self.us_side(pos) is not None:
            return live_us.quote(pos["us_slug"], pos["long"])
        return pm.best_prices(asset)

    def live_buy(self, t: dict, usd: float, us_slug: Optional[str], long: bool, ask: float, delay: int,
                 meta: Optional[dict] = None) -> None:
        if not us_slug:
            return skip(self.state, "not on Polymarket US")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        spent = self.state["live_spent_today"]
        if spent["day"] != day:
            spent.update(day=day, usd=0.0)
        usd = min(usd, float(self.settings["live_max_trade"]))
        if spent["usd"] + usd > float(self.settings["live_daily_cap"]):
            return skip(self.state, "daily cap reached")
        if usd * 1.05 > self.state["bank"]:
            return skip(self.state, "not enough cash in account")
        his_price = float(t["price"])
        cap = self.settings.get("max_slip_cents")
        # The limit is the most we'll pay: his price plus the slippage cap. IOC means
        # anything that can't fill at that price or better is cancelled, never chased.
        limit = his_price + float(cap) / 100 if cap is not None and float(cap) >= 0 else ask
        limit = min(0.99, max(limit, 0.01))
        try:
            resp = live_us.place(us_slug, long, "BUY", usd / limit, limit)
        except Exception as exc:
            log(self.state, f"Order failed: {exc}")
            return skip(self.state, "order error")
        shares, px, reported = live_us.filled(resp, long)
        if shares <= 0:
            why = live_us.rejection(resp)
            if why:
                log(self.state, f"Order rejected: {why}")
                return skip(self.state, "order rejected")
            return skip(self.state, "not filled (price moved past limit)")
        partial = shares * limit < usd * 0.99
        if partial:
            skip(self.state, "partial fill (book too thin)")
        track_slip(self.state, his_price, px)
        theta = live_us.fee_coefficient(us_slug)
        fee = taker_fee(shares, px, theta) if reported is None else round(reported, 2)
        spent["usd"] += shares * px + fee
        self.state["bank"] = max(self.state["bank"], shares * px + fee)  # the order already filled; record it in full
        buy(self.state, t, shares * px, px, delay, theta, fee=fee,
            meta={**(meta or {}), "fill": "partial" if partial else "full",
                  "fee_source": "exchange" if reported is not None else "schedule"})
        pos = self.state["positions"][str(t["asset"])]
        pos.update(us=True, us_slug=us_slug, long=long)
        pos["partial"] = pos.get("partial", 0) + partial
        self.seed_his_shares(pos, float(t.get("size") or 0), t.get("_wallet") or self.target())

    def live_sell(self, asset: str, fraction: float, why: str = "copied sell") -> None:
        pos = self.state["positions"][asset]
        if not pos.get("us_slug") or self.us_side(pos) is None:
            return skip(self.state, "can't place sell on Polymarket US")
        bid, _ = live_us.quote(pos["us_slug"], pos["long"])
        limit = max(0.01, (bid or pos.get("cur", 0.5)) - 0.02)
        try:
            resp = live_us.place(pos["us_slug"], pos["long"], "SELL", pos["shares"] * fraction, limit)
        except Exception as exc:
            log(self.state, f"Sell failed: {exc}")
            return skip(self.state, "order error")
        shares, px, reported = live_us.filled(resp, pos["long"])
        if shares <= 0:
            return skip(self.state, "sell not filled")
        sell(self.state, asset, min(1.0, shares / pos["shares"]), px, why,
             fee=None if reported is None else round(reported, 2))

    def halt(self, why: str) -> None:
        log(self.state, why)
        s, _ = self.store.read("settings.json")
        s = {**DEFAULT_SETTINGS, **(s or {}), "halted": True}
        self.store.write("settings.json", s, "Auto-halt")
        self.settings = s

    # ------------------------------------------------------------ upkeep --
    def kill_switch(self) -> None:
        log(self.state, "KILL SWITCH: selling every position")
        for asset in list(self.state["positions"]):
            if self.mode == "live":
                self.live_sell(asset, 1.0, "kill switch")
                continue
            bid, _ = self.quote(asset)
            sell(self.state, asset, 1.0, bid if bid is not None else 0.0, "kill switch")
        self.save("Kill switch")

    @staticmethod
    def us_side(pos: dict) -> Optional[bool]:
        """Whether a position is the long side of its US contract (None if that can't be told).

        Older positions didn't store this, so work it out from the outcome name; never
        assume a side, since settling on the wrong one flips a win into a loss.
        """
        if pos.get("long") is None and pos.get("us_slug"):
            pos["long"] = live_us.side_for(pos["us_slug"], pos.get("outcome"))
        return pos.get("long")

    def resolution(self, asset: str, pos: dict):
        """Settlement value (1.0/0.0) of a position, or None while the game is still open.

        Positions on Polymarket US settle when the US market resolves; anything else
        when the international market does.
        """
        if not pos.get("us_slug") and pos.get("slug") and "us_checked" not in pos:
            # Opened before US matching was stored: look the contract up once.
            pos["us_checked"] = True
            us_slug, long, _ = live_us.match(pos["slug"], pos.get("event_slug") or pos["slug"], pos.get("outcome"))
            if us_slug:
                pos.update(us_slug=us_slug, long=long, us=True)
        if pos.get("us_slug") and self.us_side(pos) is not None:
            res = live_us.settlement(pos["us_slug"], pos["long"])
            if res is not None:
                return res
        return pm.resolution(asset)

    def sync_account(self) -> None:
        """Live mode: take cash and positions from the Polymarket US account itself, so the
        dashboard matches the account even after deposits, withdrawals or outside trades."""
        acct = live_us.account()
        if acct is None:
            return log(self.state, "Couldn't reach the Polymarket US account to sync")
        acct["synced_at"] = int(time.time())
        held = live_us.positions()
        unbooked = False
        if held is not None:
            ours = {p["us_slug"]: a for a, p in self.state["positions"].items() if p.get("us_slug")}
            for slug, a in ours.items():
                pos, real = self.state["positions"][a], held.get(slug)
                qty = real["qty"] if real else 0.0
                if real and real["expired"]:
                    unbooked = True
                    continue  # settled; upkeep books the result
                if abs(qty - pos["shares"]) > max(0.01, 0.01 * pos["shares"]):
                    if qty <= 0:
                        # Gone from the account (sold in the app, or never really filled).
                        log(self.state, f"{pos['title']}: no longer in the account; closing it here")
                        bid, _ = self.quote(a)
                        sell(self.state, a, 1.0, bid or pos.get("cur", 0.0), "closed outside bot", fee=0.0)
                    else:
                        log(self.state, f"{pos['title']}: account holds {qty:.2f} shares, bot had {pos['shares']:.2f}; using the account's")
                        pos["cost"] *= qty / pos["shares"]
                        pos["shares"] = qty
                        pos["avg"] = pos["cost"] / qty
            acct["outside"] = [{"slug": s2, **r} for s2, r in held.items() if s2 not in ours and r["qty"] > 0]
        # Cash the bot can't explain is a deposit or withdrawal, not profit or loss: move the
        # starting line with it. Skipped while a settled payout hasn't been booked yet.
        moved = round(acct["buying_power"] - self.state["bank"], 2)
        if held is not None and not unbooked and abs(moved) >= 1.0:
            self.state["start_bank"] = round(self.state["start_bank"] + moved, 2)
            log(self.state, f"{'Deposit' if moved > 0 else 'Withdrawal'} of ${abs(moved):.2f} seen in the account")
        # Last, so the account's own cash figure wins over anything booked above.
        self.state["bank"] = acct["buying_power"]
        self.state["account"] = acct

    def upkeep(self) -> None:
        # Price-check a slice of positions each pass so thousands of them don't stall the loop.
        assets = sorted(self.state["positions"])
        start = self.state.get("upkeep_at", 0) % max(1, len(assets))
        batch = (assets[start:] + assets[:start])[:UPKEEP_BATCH]
        self.state["upkeep_at"] = start + len(batch)
        sh = self.shadow()
        for asset in batch:
            pos = self.state["positions"].get(asset)
            if pos is None:
                continue
            res = self.resolution(asset, pos)
            if res is not None:
                sell(self.state, asset, 1.0, res, "resolved")
                if asset in sh["positions"]:
                    sell(sh, asset, 1.0, res, "resolved")
                continue
            mid = pm.midpoint(asset)
            if mid is not None:
                pos["cur"] = mid
                if asset in sh["positions"]:
                    sh["positions"][asset]["cur"] = mid
        # Shadow-only positions (copies the filters turned down) get checked a few per pass.
        extra = [a for a in sorted(sh["positions"]) if a not in self.state["positions"]]
        if extra:
            s0 = sh.get("upkeep_at", 0) % len(extra)
            part = (extra[s0:] + extra[:s0])[:max(10, UPKEEP_BATCH // 5)]
            sh["upkeep_at"] = s0 + len(part)
            for asset in part:
                res = self.resolution(asset, sh["positions"][asset])
                if res is not None:
                    sell(sh, asset, 1.0, res, "resolved")
                else:
                    mid = pm.midpoint(asset)
                    if mid is not None:
                        sh["positions"][asset]["cur"] = mid
        self._shadow_tidy(sh)
        self.state["exposure"] = round(exposure(self.state), 2)
        if self.mode == "live":
            self.sync_account()
        if self.targets() and time.time() - getattr(self, "his_stats_at", 0) > 600:
            self.sync_traders()
            for wallet, his in self.state["traders"].items():
                pnl = pm.leaderboard_pnl(wallet)
                if "all" in pnl:
                    his.setdefault("pnl_all_at_start", pnl["all"])
                    pnl["since_copy"] = round(pnl["all"] - his["pnl_all_at_start"], 2)
                his["pnl"] = pnl
            self.his_stats_at = time.time()

    def run(self) -> None:
        start = time.time()
        last_save = last_settings = last_upkeep = 0.0
        log(self.state, f"Runner started ({self.mode})")
        while time.time() - start < RUN_SECONDS:
            now = time.time()
            try:
                if now - last_settings > 15:
                    self.refresh_settings()
                    last_settings = now
                self.poll()
                if now - last_upkeep > UPKEEP_EVERY:
                    self.upkeep()
                    last_upkeep = now
                    self.dirty = True
                # Save only when something changed (plus a slow heartbeat), so viewers
                # can poll every 15s without burning through GitHub's rate limit.
                if (self.dirty and now - last_save > SAVE_EVERY) or now - last_save > HEARTBEAT_EVERY:
                    self.save()
                    last_save = now
            except Exception as exc:  # keep running through API hiccups
                L.exception("Loop error: %s", exc)
            time.sleep(POLL)
        self.upkeep()
        self.save("Runner finished")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()
    store = LocalStore() if args.local else Store(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    Bot(store).run()


if __name__ == "__main__":
    main()
