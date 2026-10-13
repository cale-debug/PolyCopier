"""The copy loop: watch the target wallet and mirror each trade.

    python -m bot.main                 # GitHub Actions (uses GITHUB_TOKEN + repo)
    python -m bot.main --local         # on a computer, state in ./local-data

Runs for RUN_SECONDS, then exits so the workflow can start a fresh run.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Optional

from . import live_us, polymarket as pm
from .portfolio import (DEFAULT_SETTINGS, bet_size, buy, exposure, log, mark, new_state,
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
        if self.settings.get("mode") == "live":
            if live_us.has_keys():
                return "live"
            L.warning("Live mode requested but no Polymarket US keys in secrets; staying on paper")
        return "paper"

    def state_path(self) -> str:
        return f"state-{self.mode}.json"

    def load_state(self) -> dict:
        st, _ = self.store.read(self.state_path())
        if st is None:
            bank = float(self.settings["paper_start_bank"])
            if self.mode == "live":
                bank = live_us.balance() or 0.0
            st = new_state(bank)
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
        st["mode"] = self.mode
        return st

    def save(self, why: str = "Update state") -> None:
        self.state["heartbeat"] = int(time.time())
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
    def target(self) -> str:
        return str(self.settings.get("target", "")).lower()

    def poll(self) -> None:
        cur = self.state["cursor"]
        if cur.get("target") != self.target():
            # New target: follow from now on, don't replay his history.
            self.state["cursor"] = cur = {"target": self.target(), "ts": int(time.time()), "seen": []}
            self.state["his"] = {"since": int(time.time()), "buys": 0, "sells": 0, "usd": 0.0}
            self.his_stats_at = 0
            log(self.state, f"Now copying {self.settings.get('target_label') or self.target()}")
            self.dirty = True
        if not self.target():
            return
        trades = pm.new_trades(self.target(), cur["ts"])
        if trades:
            L.info("%d trade rows from target since %d", len(trades), cur["ts"])
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
            his = self.state.setdefault("his", {"since": cur["ts"], "buys": 0, "sells": 0, "usd": 0.0})
            his["buys" if str(t.get("side")).upper() == "BUY" else "sells"] += 1
            his["usd"] = round(his["usd"] + float(t.get("usdcSize") or 0), 2)
            self.copy(t, max(0, int(time.time()) - ts))
            self.dirty = True
        cur["seen"] = list(seen)[-200:]

    def copy(self, t: dict, delay: int) -> None:
        side = str(t.get("side")).upper()
        asset = str(t.get("asset"))
        his_usd = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * float(t.get("price") or 0)
        us_slug = live_us.us_market(t.get("slug") or "", t.get("eventSlug") or "")
        on_us = us_slug is not None
        his_size = float(t.get("size") or 0)
        if side == "BUY":
            self.state["us"]["checked"] += 1
            self.state["us"]["available"] += on_us
            held = self.state["positions"].get(asset)
            if held is not None and "his_shares" in held:
                held["his_shares"] += his_size  # so we know what share of his stack a later sell is
        if side == "BUY":
            why = should_copy_buy(self.settings, his_usd)
            if why:
                return skip(self.state, why)
            if self.settings.get("us_only", True) and not on_us:
                return skip(self.state, "not on Polymarket US")
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
            long = live_us.is_long(us_slug, t.get("outcome"), t.get("outcomeIndex")) if us_slug else True
            his_price = float(t.get("price") or 0)
            # Price off the book we'd really trade on: Polymarket US when the market is there.
            _, ask = live_us.quote(us_slug, long) if us_slug else pm.best_prices(asset)
            if ask is None:
                return skip(self.state, "no sellers")
            if not slip_ok(self.settings, his_price, ask):
                self.state.setdefault("slip", {"n": 0, "cents": 0.0, "skipped": 0})["skipped"] += 1
                return skip(self.state, "price moved past slippage cap")
            if self.mode == "live":
                return self.live_buy(t, usd, us_slug, long, ask, delay)
            fill = buy(self.state, t, usd, ask, delay, live_us.fee_coefficient(us_slug))
            if fill:
                track_slip(self.state, his_price, ask)
                pos = self.state["positions"][asset]
                pos.update(us=on_us, us_slug=us_slug, long=long)
                self.seed_his_shares(pos, his_size)
                L.info("BUY  $%.2f %s %s @ %.3f (he paid %.3f, %ds late)", usd, t.get("outcome"),
                       t.get("title"), ask, his_price, delay)
        elif side == "SELL":
            # Exits are followed even while halted: halting stops new bets, not getting out.
            if not self.settings.get("copy_sells") or asset not in self.state["positions"]:
                return
            pos = self.state["positions"][asset]
            sold = float(t.get("size") or 0)
            before = pos.get("his_shares")
            if not before:
                before = sold + pm.his_position_shares(self.target(), asset)
            fraction = min(1.0, sold / before) if before > 0 else 1.0
            pos["his_shares"] = max(0.0, before - sold)
            if pos["his_shares"] < 1e-6:
                fraction = 1.0  # he's fully out, so are we
            if self.mode == "live":
                return self.live_sell(asset, fraction)
            bid, _ = self.quote(asset)
            if bid is None:
                return skip(self.state, "no buyers")
            sell(self.state, asset, fraction, bid, "copied sell")
            L.info("SELL %.0f%% of %s @ %.3f", fraction * 100, t.get("title"), bid)

    def seed_his_shares(self, pos: dict, his_size: float) -> None:
        """On our first buy of an outcome, note how many shares he holds (including any from before we started)."""
        if "his_shares" not in pos:
            pos["his_shares"] = max(pm.his_position_shares(self.target(), pos["asset"]), his_size)

    # ---------------------------------------------------------------- live --
    def quote(self, asset: str):
        """(bid, ask) for a position we hold, from the book it trades on."""
        pos = self.state["positions"].get(asset) or {}
        if pos.get("us_slug"):
            return live_us.quote(pos["us_slug"], pos.get("long", True))
        return pm.best_prices(asset)

    def live_buy(self, t: dict, usd: float, us_slug: Optional[str], long: bool, ask: float, delay: int) -> None:
        if not us_slug:
            return skip(self.state, "not on Polymarket US")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        spent = self.state["live_spent_today"]
        if spent["day"] != day:
            spent.update(day=day, usd=0.0)
        usd = min(usd, float(self.settings["live_max_trade"]))
        if spent["usd"] + usd > float(self.settings["live_daily_cap"]):
            return skip(self.state, "daily cap reached")
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
        shares, px = live_us.filled(resp)
        if shares <= 0:
            return skip(self.state, "not filled")
        track_slip(self.state, his_price, px)
        theta = live_us.fee_coefficient(us_slug)
        fee = taker_fee(shares, px, theta)
        spent["usd"] += shares * px + fee
        self.state["bank"] = max(self.state["bank"], shares * px + fee)  # the order already filled; record it in full
        buy(self.state, t, shares * px, px, delay, theta)
        pos = self.state["positions"][str(t["asset"])]
        pos.update(us=True, us_slug=us_slug, long=long)
        self.seed_his_shares(pos, float(t.get("size") or 0))

    def live_sell(self, asset: str, fraction: float, why: str = "copied sell") -> None:
        pos = self.state["positions"][asset]
        if not pos.get("us_slug"):
            return skip(self.state, "not on Polymarket US")
        bid, _ = live_us.quote(pos["us_slug"], pos.get("long", True))
        limit = max(0.01, (bid or pos.get("cur", 0.5)) - 0.02)
        try:
            resp = live_us.place(pos["us_slug"], pos.get("long", True), "SELL", pos["shares"] * fraction, limit)
        except Exception as exc:
            log(self.state, f"Sell failed: {exc}")
            return skip(self.state, "order error")
        shares, px = live_us.filled(resp)
        if shares > 0:
            sell(self.state, asset, min(1.0, shares / pos["shares"]), px, why)

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

    def upkeep(self) -> None:
        # Price-check a slice of positions each pass so thousands of them don't stall the loop.
        assets = sorted(self.state["positions"])
        start = self.state.get("upkeep_at", 0) % max(1, len(assets))
        batch = (assets[start:] + assets[:start])[:UPKEEP_BATCH]
        self.state["upkeep_at"] = start + len(batch)
        for asset in batch:
            pos = self.state["positions"].get(asset)
            if pos is None:
                continue
            res = pm.resolution(asset)
            if res is not None:
                sell(self.state, asset, 1.0, res, "resolved")
                continue
            mid = pm.midpoint(asset)
            if mid is not None:
                pos["cur"] = mid
        self.state["exposure"] = round(exposure(self.state), 2)
        if self.target() and time.time() - getattr(self, "his_stats_at", 0) > 600:
            his = self.state.setdefault("his", {"since": int(time.time()), "buys": 0, "sells": 0, "usd": 0.0})
            pnl = pm.leaderboard_pnl(self.target())
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
