# Football Betting Telegram Bot ⚽

A Telegram bot where players bet virtual money on football match results
(1X2 — home / draw / away) at real bookmaker odds. Ships configured for the
**English Premier League**, but any competition works via `SPORT_KEY` /
`LEAGUE_NAME` (see [Switching competitions](#switching-competitions)).
Everyone starts with **$10,000**. Matches, odds, and final scores all come from
[The Odds API](https://the-odds-api.com); finished matches settle automatically.

## How it works

- Odds and fixtures are pulled from The Odds API (`/odds` endpoint).
- Fixtures & odds **auto-refresh** on a timer (`SYNC_INTERVAL`, default 6h), so
  each new gameweek appears without anyone running `/sync`.
- Players bet a stake on home/draw/away; the odds are **snapshotted** onto the
  bet, so later odds movements don't change a placed bet.
- A background job polls The Odds API (`/scores`); when a match completes,
  winning bets are paid `stake × odds` and the result is announced to the chat.
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
5. **Install & run** (uses [uv](https://docs.astral.sh/uv/)):
   ```bash
   uv sync        # creates .venv and installs locked dependencies
   uv run bot.py  # start the bot
   ```
   Don't have uv? `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Switching competitions

The bot isn't tied to one league. To point it elsewhere, set two env vars in
`.env` (keep them in sync) and restart:

```bash
SPORT_KEY=soccer_uefa_champs_league   # The Odds API sport key
LEAGUE_NAME=Champions League           # name shown in bot messages
```

Common keys: `soccer_epl`, `soccer_uefa_champs_league`, `soccer_spain_la_liga`,
`soccer_italy_serie_a`, `soccer_fifa_world_cup`. Full list:
`https://api.the-odds-api.com/v4/sports?apiKey=YOUR_KEY`

> Switching competitions mid-database is fine — old settled matches just stay in
> history. For a clean slate (fresh balances), point `DB_PATH` at a new file.

## Commands

| Command | Who | Description |
|---|---|---|
| `/start` | all | Register and get your starting balance |
| `/matches` | all | List upcoming matches, codes & odds (tap to bet) |
| `/bet <code> <home\|draw\|away> <amount>` | all | Place a bet |
| `/mybets` | all | Your open bets |
| `/bets` | all | Everyone's bets on live & upcoming matches |
| `/history` | all | Your settled bet results |
| `/balance` | all | Your balance |
| `/leaderboard` | all | Top players by net worth |
| `/cancel` | all | Cancel the in-progress betting flow |
| `/help` | all | Show the command list |
| `/sync` | admin | Fetch fixtures & odds from The Odds API |
| `/settle` | admin | Force a settlement check now |
| `/reset <@user\|id> [amount]` | admin | Set a player's balance |

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
  credits and an odds refresh (`/sync` or the auto-sync job) costs 1.
  - **Settlement** only calls the API **while a match is in a live window**
    (kicked off, not yet settled, within 12h) — outside those windows it spends
    nothing, so cost scales with hours of live football, not wall-clock time.
  - **Auto-sync** runs unconditionally on `SYNC_INTERVAL`. The default 6h =
    ~4 credits/day ≈ **120/month**, which is fine for the free tier but is now
    the steady baseline cost (a league plays all season, unlike a 1-month cup).
    Raise `SYNC_INTERVAL` (e.g. 43200 = 12h) if you want to spend less; lower it
    if you want new odds to appear faster.
  - A full EPL season is heavier than a World Cup. If you bump into the cap,
    raise `SYNC_INTERVAL`, widen `SETTLE_INTERVAL`, or grab a cheap paid tier.
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
