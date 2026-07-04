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

SPORT_KEY = os.getenv("SPORT_KEY", "soccer_fifa_world_cup")
ODDS_REGION = os.getenv("ODDS_REGION", "eu")

STARTING_BALANCE = int(os.getenv("STARTING_BALANCE", "10000"))
SETTLE_INTERVAL = int(os.getenv("SETTLE_INTERVAL", "600"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "21600"))  # fixture/odds refresh, 6h
DB_PATH = os.getenv("DB_PATH", "worldcup.db")
