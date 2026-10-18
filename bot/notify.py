"""Telegram alerts for the bot's own fills (buys, sells, settlements).

Needs TELEGRAM_BOT_TOKEN (a GitHub Secret). The chat to message is found by itself:
the first time someone sends the Telegram bot any message, that chat is remembered
in state["telegram"]["chat"]. TELEGRAM_CHAT_ID, if set, wins.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

L = logging.getLogger("bot")
API = "https://api.telegram.org/bot{token}/{method}"
FIND_CHAT_EVERY = 30  # seconds between looks for the chat while it isn't known yet


def _call(token: str, method: str, **params) -> Optional[dict]:
    try:
        r = requests.post(API.format(token=token, method=method), json=params, timeout=10)
        body = r.json()
        return body.get("result") if body.get("ok") else None
    except Exception as exc:
        L.warning("Telegram %s failed: %s", method, exc)
        return None


def money(x: float) -> str:
    return f"${x:,.2f}"


def describe(rows: list, mode: str) -> str:
    """One message for a batch of fills on the same bet (a big entry often fills in pieces)."""
    r0 = rows[0]
    side, usd = r0.get("side"), sum(float(r.get("usd") or 0) for r in rows)
    shares = sum(float(r.get("shares") or 0) for r in rows)
    who = r0.get("who") or ""
    tag = "" if mode == "live" else "PAPER · "
    bet = f"{r0.get('title', '')}\n{r0.get('outcome', '')}"
    if side == "BUY":
        avg = usd / shares if shares else float(r0.get("price") or 0)
        head = f"🟢 {tag}Bought {money(usd)} at {avg * 100:.0f}¢"
    elif side == "SELL":
        head = f"🔴 {tag}Sold for {money(usd)}"
    else:
        won = any(float(r.get("price") or 0) > 0.5 for r in rows)
        head = f"{'✅' if won else '❌'} {tag}Settled: {'won' if won else 'lost'}, paid {money(usd)}"
    return f"{head}\n{bet}" + (f"\nfrom {who}" if who else "")


class Notifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
        self.last_find = 0.0

    @property
    def on(self) -> bool:
        return bool(self.token)

    def chat(self, tg: dict) -> Optional[str]:
        self.chat_id = self.chat_id or tg.get("chat")
        if self.chat_id or time.time() - self.last_find < FIND_CHAT_EVERY:
            return self.chat_id
        self.last_find = time.time()
        for u in reversed(_call(self.token, "getUpdates") or []):
            c = ((u.get("message") or {}).get("chat") or {}).get("id")
            if c:
                self.chat_id = str(c)
                _call(self.token, "sendMessage", chat_id=self.chat_id,
                      text="PolyCopier alerts are on. You'll get a message here for every bet the bot places, sells or settles.")
                break
        return self.chat_id

    def send(self, text: str) -> bool:
        return bool(self.chat_id and _call(self.token, "sendMessage", chat_id=self.chat_id, text=text,
                                           disable_web_page_preview=True))

    def flush(self, state: dict, mode: str) -> bool:
        """Send every fill newer than the last one sent. True if state changed."""
        if not self.on:
            return False
        tg = state.setdefault("telegram", {})
        if "sent_ts" not in tg:
            tg["sent_ts"] = int(time.time())  # never replay history the first time
            return True
        changed = False
        if self.chat(tg) and tg.get("chat") != self.chat_id:
            # Just connected: start from now rather than replaying everything since setup.
            tg.update(chat=self.chat_id, sent_ts=int(time.time()))
            changed = True
        new = [r for r in state.get("trades", []) if int(r.get("ts", 0)) > tg["sent_ts"]]
        if not new or not self.chat_id:
            return changed
        new.sort(key=lambda r: r.get("ts", 0))
        groups: dict = {}
        for r in new:
            groups.setdefault((r.get("side"), r.get("title"), r.get("outcome")), []).append(r)
        for rows in groups.values():
            if not self.send(describe(rows, mode)):
                return changed  # try again next pass; nothing marked sent
        tg["sent_ts"] = int(new[-1].get("ts", 0))
        return True
