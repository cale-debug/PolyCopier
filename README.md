# PolyCopier

A Polymarket copy-trader. Pick any trader's wallet, and PolyCopier mirrors their
trades: paper money first, real money on Polymarket US when you say so.
Everything runs on GitHub's free tier.

- **The bot** runs on GitHub Actions around the clock (free and unlimited for public repos).
  It checks the target wallet every 10 seconds and copies each buy and sell.
- **The dashboard** is a GitHub Pages site you can add to your phone's home screen.
  It shows bank, exposure, P&L, win/loss and open positions, and has the controls:
  target, size mode, min bet, copy sells, HALT and the kill switch.
- **State** lives on the repo's `data` branch: `settings.json` (what the dashboard
  sets) and `state-paper.json` / `state-live.json` (what the bot did).

## Setup (once)

1. **Repo is public.** Free unlimited Actions minutes and free Pages need it.
   No keys are ever stored in the code.
2. **Turn on Pages.** Settings → Pages → Source: **GitHub Actions**.
   Then Actions → *Dashboard* → *Run workflow*. Your dashboard will be at
   `https://<your-username>.github.io/<repo>/`.
3. **Start the bot.** Actions → *Copy bot* → *Run workflow*. After that it keeps
   itself running on a schedule.
4. **Make a token for the dashboard's buttons.** GitHub → Settings → Developer
   settings → Fine-grained tokens → Generate. Repository access: only this repo.
   Permissions: **Contents**, **Secrets** and **Actions**, each read and write.
   Paste it in the dashboard's SETUP tab. It's stored only on that device.
5. **Phone app.** Open the dashboard in Safari → Share → Add to Home Screen.

## Paper test

- The bot starts copying **e46m3** with $1 per trade from a $1,000 paper bank,
  and only copies trades in markets that also exist on Polymarket US
  (toggle: "Polymarket US only"), so the paper result matches what a US account can do.
- Change the trader by typing a username or 0x wallet in TARGET and pressing
  LOCK TARGET. It follows the new trader from that moment on.
- Paper buys fill at the real best ask when the bot sees the trade, and sells
  at the real best bid, so delay and spread are counted.
- "On Polymarket US: X of Y" shows how many of the trader's buys are in markets
  that also exist on Polymarket US, which is all a US account can copy for real.

## Going live (real money)

Only after the paper test is profitable:

1. Open an account at polymarket.us and deposit.
2. Create API keys at polymarket.us/developer.
3. Dashboard → GO LIVE → paste the key ID and secret → SAVE KEYS.
   They're encrypted in the browser and stored as GitHub Secrets.
4. Set **Max per trade** and **Max per day** low for the first days.
5. Press GO LIVE and type `LIVE`. The bot restarts in live mode with its own
   live P&L, separate from the paper record. BACK TO PAPER stops real trading.

Live orders are immediate-or-cancel limit orders at most 2¢ worse than the
price the bot sees, so it never chases a price.

## Run it on a computer instead

```bash
pip install -r requirements.txt
python -m bot.main --local      # state in ./local-data
```

## Tests

```bash
pip install pytest && python -m pytest -q
```

*Not financial advice. Copy-trading can lose money; only risk what you can lose.*
