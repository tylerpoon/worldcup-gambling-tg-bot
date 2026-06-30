"""Client for The Odds API (https://the-odds-api.com).

We use two endpoints, both keyed by SPORT_KEY:
  - /odds   -> upcoming events with bookmaker h2h (1X2) odds
  - /scores -> recent/live events with final scores + a `completed` flag
"""
from datetime import datetime, timezone
from typing import Optional

import httpx

import config

BASE = "https://api.the-odds-api.com/v4"


def _iso_to_unix(iso: str) -> int:
    # The Odds API returns e.g. "2026-06-11T19:00:00Z"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.astimezone(timezone.utc).timestamp())


def _avg_h2h_odds(event: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Average h2h odds across all bookmakers for home/draw/away."""
    home, draw, away = event["home_team"], None, event["away_team"]
    sums = {"home": 0.0, "draw": 0.0, "away": 0.0}
    counts = {"home": 0, "draw": 0, "away": 0}

    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name, price = outcome.get("name"), outcome.get("price")
                if price is None:
                    continue
                if name == home:
                    sums["home"] += price; counts["home"] += 1
                elif name == away:
                    sums["away"] += price; counts["away"] += 1
                elif name and name.lower() == "draw":
                    sums["draw"] += price; counts["draw"] += 1

    def avg(k):
        return round(sums[k] / counts[k], 2) if counts[k] else None

    return avg("home"), avg("draw"), avg("away")


async def fetch_odds() -> list[dict]:
    """Return normalized upcoming matches with averaged 1X2 odds."""
    url = f"{BASE}/sports/{config.SPORT_KEY}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": config.ODDS_REGION,
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        events = resp.json()

    out = []
    for ev in events:
        oh, od, oa = _avg_h2h_odds(ev)
        out.append(
            {
                "match_id": ev["id"],
                "home": ev["home_team"],
                "away": ev["away_team"],
                "kickoff": _iso_to_unix(ev["commence_time"]),
                "odds_home": oh,
                "odds_draw": od,
                "odds_away": oa,
            }
        )
    return out


async def fetch_scores(days_from: int = 1) -> dict[str, dict]:
    """Return {match_id: {home_score, away_score, completed}} for recent events."""
    url = f"{BASE}/sports/{config.SPORT_KEY}/scores"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "daysFrom": days_from,
        "dateFormat": "iso",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        events = resp.json()

    result: dict[str, dict] = {}
    for ev in events:
        scores = ev.get("scores")
        home_score = away_score = None
        if scores:
            by_name = {s["name"]: s["score"] for s in scores}
            try:
                home_score = int(by_name.get(ev["home_team"]))
                away_score = int(by_name.get(ev["away_team"]))
            except (TypeError, ValueError):
                home_score = away_score = None
        result[ev["id"]] = {
            "home_score": home_score,
            "away_score": away_score,
            "completed": bool(ev.get("completed")),
        }
    return result
