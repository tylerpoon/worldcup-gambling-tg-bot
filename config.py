"""Configuration loaded from environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


BOT_TOKEN = _require("BOT_TOKEN")
ODDS_API_KEY = _require("ODDS_API_KEY")

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

SPORT_KEY = os.getenv("SPORT_KEY", "soccer_epl")
# Human-friendly competition name shown in bot messages. Change this and
# SPORT_KEY together to point the bot at a different competition.
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "Premier League")
ODDS_REGION = os.getenv("ODDS_REGION", "uk")

STARTING_BALANCE = int(os.getenv("STARTING_BALANCE", "10000"))
SETTLE_INTERVAL = int(os.getenv("SETTLE_INTERVAL", "600"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "21600"))  # fixture/odds refresh, 6h

# How often each player is topped up (seconds). Default: one week. The payout
# amount is set at runtime by an admin with /allowance; 0 (the default) is off.
ALLOWANCE_INTERVAL = int(os.getenv("ALLOWANCE_INTERVAL", str(7 * 24 * 3600)))
# How often the bot checks whether an allowance payout is due.
ALLOWANCE_CHECK_INTERVAL = int(os.getenv("ALLOWANCE_CHECK_INTERVAL", "3600"))

DB_PATH = os.getenv("DB_PATH", "epl.db")
