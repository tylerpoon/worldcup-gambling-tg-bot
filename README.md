# World Cup Betting Telegram Bot ⚽

A Telegram bot where players bet virtual money on World Cup match results
(1X2 — home / draw / away) at real bookmaker odds. Everyone starts with
**$10,000**. Matches, odds, and final scores all come from
[The Odds API](https://the-odds-api.com); finished matches settle automatically.

## How it works

- Odds and fixtures are pulled from The Odds API (`/odds` endpoint).
- Players bet a stake on home/draw/away; the odds are **snapshotted** onto the
  bet, so later odds movements don't change a placed bet.
- A background job polls The Odds API (`/scores`) every few minutes; when a
  match completes, winning bets are paid `stake × odds` and the result is
  announced.
- Data lives in a local SQLite file.

## Setup

1. **Create the bot**: message [@BotFather](https://t.me/BotFather) →
   `/newbot` → copy the token.
2. **Get your admin id**: message [@userinfobot](https://t.me/userinfobot) →
   copy your numeric id.
3. **Get an Odds API key**: sign up at https://the-odds-api.com (free tier).
4. **Configure**:
   ```bash
   cp .env.example .env
   # edit .env: BOT_TOKEN, ADMIN_IDS, ODDS_API_KEY
   ```
5. **Install & run**:
   ```bash
   pip install -r requirements.txt
   python bot.py
   ```

> When the World Cup isn't running, set `SPORT_KEY` in `.env` to an active
> competition (e.g. `soccer_epl`) to test against live data. See the full list:
> `https://api.the-odds-api.com/v4/sports?apiKey=YOUR_KEY`

## Commands

| Command | Who | Description |
|---|---|---|
| `/start` | all | Register and get your starting balance |
| `/matches` | all | List upcoming matches, codes & odds |
| `/bet <code> <home\|draw\|away> <amount>` | all | Place a bet |
| `/mybets` | all | Your open bets |
| `/balance` | all | Your balance |
| `/leaderboard` | all | Top players |
| `/reset` | all | Reset back to the starting balance |
| `/sync` | admin | Fetch fixtures & odds from The Odds API |
| `/settle` | admin | Force a settlement check now |

### Two ways to bet
- **Tap-to-bet (inline buttons):** run `/matches`, tap a match → tap a result
  (with its odds) → tap a stake (presets or **All-in**). Done in three taps.
- **Command:** `/bet <code> <home|draw|away> <amount>` using the short 8-character
  code shown in `/matches`, e.g. `/bet 0d8a1f2b home 500` (handy for custom amounts).

### Result announcements
When a match finishes, the bot posts the score, the winner, and a per-player
payout breakdown **into each chat where bets on that match were placed** — so
your group sees results automatically. Bets placed in a DM are announced in that
DM. (Legacy bets with no recorded chat fall back to the first admin.)

> **Group privacy mode:** the inline buttons and all commands work in groups out
> of the box. If you want the bot to also read non-command text in a group,
> disable privacy mode via @BotFather (`/setprivacy`). It's not required here.

## Notes & next steps

- **API quota** (free tier ≈ 500 credits/month): a `/scores` poll costs 2
  credits and a `/sync` costs 1. To stay free, the settle job **only calls the
  API while a match is in a live window** (kicked off, not yet settled, within
  12h of kickoff) — outside those windows it spends nothing. So you can keep a
  responsive `SETTLE_INTERVAL=1800` (30 min) and cost scales with hours of live
  football, not wall-clock time. If you ever remove that guard, use
  `SETTLE_INTERVAL=14400` (4h) to stay under the free tier with 24/7 polling.
- Possible extensions: exact-score and over/under markets, daily top-ups,
  per-group leaderboards, a "custom stake" button (works best in DMs or with
  group privacy mode off).

## Project layout

```
bot.py        # handlers, settlement job, entrypoint
db.py         # SQLite data layer
odds_api.py   # The Odds API client (odds + scores)
config.py     # env / .env loading
```
