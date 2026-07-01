"""SQLite data layer.

Small, synchronous helpers. Operations are tiny and fast, so we call them
directly from async handlers without a separate thread pool. A single shared
connection (check_same_thread=False) is fine for a friends-group bot.
"""
import sqlite3
import time
from typing import Optional

import config

_conn: Optional[sqlite3.Connection] = None


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init() -> None:
    c = connect()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            balance    REAL NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS matches (
            match_id    TEXT PRIMARY KEY,      -- event id from The Odds API
            home        TEXT NOT NULL,
            away        TEXT NOT NULL,
            kickoff     INTEGER NOT NULL,       -- unix seconds (commence_time)
            status      TEXT NOT NULL DEFAULT 'SCHEDULED',  -- SCHEDULED/FINISHED/SETTLED
            home_score  INTEGER,
            away_score  INTEGER,
            odds_home   REAL,
            odds_draw   REAL,
            odds_away   REAL,
            updated_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bets (
            bet_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(user_id),
            match_id    TEXT NOT NULL REFERENCES matches(match_id),
            selection   TEXT NOT NULL,          -- HOME/DRAW/AWAY
            stake       REAL NOT NULL,
            odds_at_bet REAL NOT NULL,
            status      TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN/WON/LOST/VOID
            payout      REAL NOT NULL DEFAULT 0,
            chat_id     INTEGER,                -- chat where the bet was placed
            created_at  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id);
        CREATE INDEX IF NOT EXISTS idx_bets_match ON bets(match_id, status);
        """
    )
    _migrate(c)
    c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns introduced after a database may already exist."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(bets)")}
    if "chat_id" not in cols:
        c.execute("ALTER TABLE bets ADD COLUMN chat_id INTEGER")


def now() -> int:
    return int(time.time())


# ---------------- users ----------------

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM users WHERE user_id=?", (user_id,)
    ).fetchone()


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM users WHERE lower(username)=lower(?)", (username,)
    ).fetchone()


def create_user(user_id: int, username: str, balance: float) -> sqlite3.Row:
    c = connect()
    c.execute(
        "INSERT INTO users (user_id, username, balance, created_at) VALUES (?,?,?,?)",
        (user_id, username, balance, now()),
    )
    c.commit()
    return get_user(user_id)


def ensure_user(user_id: int, username: str, starting_balance: float) -> sqlite3.Row:
    """Return existing user, or create one with the starting balance."""
    user = get_user(user_id)
    if user is None:
        return create_user(user_id, username, starting_balance)
    # keep username fresh
    if username and username != user["username"]:
        c = connect()
        c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        c.commit()
    return user


def set_balance(user_id: int, balance: float) -> None:
    c = connect()
    c.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, user_id))
    c.commit()


def adjust_balance(user_id: int, delta: float) -> None:
    c = connect()
    c.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id)
    )
    c.commit()


def leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    """Players ranked by net worth (cash balance + stake locked in open bets).

    Each row also carries `staked` (total open-bet stake) so callers can show
    how much of the net worth is tied up in bets.
    """
    return connect().execute(
        """SELECT u.user_id, u.username, u.balance,
                  COALESCE(SUM(CASE WHEN b.status='OPEN' THEN b.stake END), 0) AS staked
           FROM users u
           LEFT JOIN bets b ON b.user_id = u.user_id
           GROUP BY u.user_id, u.username, u.balance
           ORDER BY (u.balance + staked) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


# ---------------- matches ----------------

def upsert_match(
    match_id: str,
    home: str,
    away: str,
    kickoff: int,
    odds_home: Optional[float],
    odds_draw: Optional[float],
    odds_away: Optional[float],
) -> None:
    """Insert a new match or refresh odds/teams for an existing one.

    Never overwrites score/status of a match that is already FINISHED/SETTLED.
    """
    c = connect()
    existing = c.execute(
        "SELECT status FROM matches WHERE match_id=?", (match_id,)
    ).fetchone()
    if existing is None:
        c.execute(
            """INSERT INTO matches
               (match_id, home, away, kickoff, odds_home, odds_draw, odds_away, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (match_id, home, away, kickoff, odds_home, odds_draw, odds_away, now()),
        )
    else:
        c.execute(
            """UPDATE matches
               SET home=?, away=?, kickoff=?,
                   odds_home=?, odds_draw=?, odds_away=?, updated_at=?
               WHERE match_id=?""",
            (home, away, kickoff, odds_home, odds_draw, odds_away, now(), match_id),
        )
    c.commit()


def get_match(match_id: str) -> Optional[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM matches WHERE match_id=?", (match_id,)
    ).fetchone()


def resolve_match(code: str) -> list[sqlite3.Row]:
    """Find matches by full id or by a short id prefix (8-char display code)."""
    code = code.strip().lower()
    return connect().execute(
        "SELECT * FROM matches WHERE lower(match_id)=? OR lower(match_id) LIKE ?",
        (code, code + "%"),
    ).fetchall()


def upcoming_matches(now_ts: int) -> list[sqlite3.Row]:
    """Matches that haven't kicked off yet, soonest first."""
    return connect().execute(
        "SELECT * FROM matches WHERE kickoff > ? AND status='SCHEDULED' "
        "ORDER BY kickoff ASC",
        (now_ts,),
    ).fetchall()


def live_matches(now_ts: int) -> list[sqlite3.Row]:
    """Matches that have kicked off but aren't settled yet (in play)."""
    return connect().execute(
        "SELECT * FROM matches WHERE kickoff <= ? AND status='SCHEDULED' "
        "ORDER BY kickoff ASC",
        (now_ts,),
    ).fetchall()


def record_result(match_id: str, home_score: int, away_score: int) -> None:
    c = connect()
    c.execute(
        "UPDATE matches SET home_score=?, away_score=?, status='FINISHED', updated_at=? "
        "WHERE match_id=?",
        (home_score, away_score, now(), match_id),
    )
    c.commit()


def matches_to_settle() -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM matches WHERE status='FINISHED'"
    ).fetchall()


def has_matches_in_play(now_ts: int, window_hours: int = 12) -> bool:
    """True if any match has kicked off but isn't settled yet (within a window).

    Used to gate score-polling: outside live windows there is nothing to settle,
    so we skip the API call entirely and spend no credits. The window stops us
    polling forever for a match that was postponed/cancelled and never completes.
    """
    row = connect().execute(
        "SELECT 1 FROM matches "
        "WHERE status != 'SETTLED' AND kickoff <= ? AND kickoff > ? LIMIT 1",
        (now_ts, now_ts - window_hours * 3600),
    ).fetchone()
    return row is not None


def mark_settled(match_id: str) -> None:
    c = connect()
    c.execute("UPDATE matches SET status='SETTLED' WHERE match_id=?", (match_id,))
    c.commit()


# ---------------- bets ----------------

def create_bet(
    user_id: int,
    match_id: str,
    selection: str,
    stake: float,
    odds_at_bet: float,
    chat_id: Optional[int] = None,
) -> int:
    c = connect()
    cur = c.execute(
        """INSERT INTO bets
           (user_id, match_id, selection, stake, odds_at_bet, chat_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, match_id, selection, stake, odds_at_bet, chat_id, now()),
    )
    c.commit()
    return cur.lastrowid


def get_bet(bet_id: int) -> Optional[sqlite3.Row]:
    return connect().execute(
        """SELECT b.*, u.username, m.home, m.away
           FROM bets b
           JOIN users u ON b.user_id = u.user_id
           JOIN matches m ON b.match_id = m.match_id
           WHERE b.bet_id=?""",
        (bet_id,),
    ).fetchone()


def open_bets_for_user(user_id: int) -> list[sqlite3.Row]:
    return connect().execute(
        """SELECT b.*, m.home, m.away, m.kickoff
           FROM bets b JOIN matches m ON b.match_id = m.match_id
           WHERE b.user_id=? AND b.status='OPEN'
           ORDER BY m.kickoff ASC""",
        (user_id,),
    ).fetchall()


def settled_bets_for_user(user_id: int, limit: int = 15) -> list[sqlite3.Row]:
    """Most recent settled (WON/LOST) bets for a user, newest first."""
    return connect().execute(
        """SELECT b.*, m.home, m.away, m.home_score, m.away_score
           FROM bets b JOIN matches m ON b.match_id = m.match_id
           WHERE b.user_id=? AND b.status IN ('WON','LOST')
           ORDER BY b.bet_id DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()


def open_bets_grouped() -> list[sqlite3.Row]:
    """Open bets aggregated per (match, outcome, user) with their total stake.

    Open bets only exist on unsettled matches, so this covers both upcoming and
    in-play matches. Used by /bets to show where the money is.
    """
    return connect().execute(
        """SELECT b.match_id, b.selection, u.username, SUM(b.stake) AS total
           FROM bets b
           JOIN users u ON b.user_id = u.user_id
           WHERE b.status='OPEN'
           GROUP BY b.match_id, b.selection, u.user_id
           ORDER BY total DESC"""
    ).fetchall()


def open_bets_for_match(match_id: str) -> list[sqlite3.Row]:
    return connect().execute(
        """SELECT b.*, u.username
           FROM bets b JOIN users u ON b.user_id = u.user_id
           WHERE b.match_id=? AND b.status='OPEN'""",
        (match_id,),
    ).fetchall()


def settle_bet(bet_id: int, status: str, payout: float) -> None:
    c = connect()
    c.execute(
        "UPDATE bets SET status=?, payout=? WHERE bet_id=?", (status, payout, bet_id)
    )
    c.commit()
