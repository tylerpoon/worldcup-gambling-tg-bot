"""Football betting Telegram bot.

Players start with a virtual balance and bet on match results (1X2) at fixed
odds pulled from The Odds API. Finished matches are settled automatically.
The competition is configured via SPORT_KEY / LEAGUE_NAME (default: EPL).
"""
import html
import logging
import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

PRESET_STAKES = [100, 500, 1000, 5000]

import config
import db
import odds_api

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("betting-bot")

SELECTIONS = {
    "home": "HOME",
    "1": "HOME",
    "draw": "DRAW",
    "x": "DRAW",
    "away": "AWAY",
    "2": "AWAY",
}


# ---------------- helpers ----------------

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def money(x: float) -> str:
    return f"${x:,.0f}"


def short(match_id: str) -> str:
    return match_id[:8]


DISPLAY_TZ = ZoneInfo("America/New_York")


def kickoff_str(ts: int) -> str:
    # %Z renders EDT/EST as appropriate for the date.
    return datetime.fromtimestamp(ts, tz=DISPLAY_TZ).strftime("%a %d %b %H:%M %Z")


def display_name(update: Update) -> str:
    u = update.effective_user
    return u.username or u.first_name or str(u.id)


def reg(update: Update):
    """Ensure the calling user exists; return their row."""
    return db.ensure_user(
        update.effective_user.id, display_name(update), config.STARTING_BALANCE
    )


def sel_label(m, selection: str) -> str:
    return {"HOME": m["home"], "DRAW": "Draw", "AWAY": m["away"]}[selection]


# ---------------- inline keyboards ----------------
# Callback data is ':' separated. <o> is the id of the user who ran /matches;
# only that user may click (enforced in on_callback). match_id is hex (no
# colons), selection is HOME/DRAW/AWAY, amount is a number or "all". The longest
# payload (s:<o>:<match_id>:<SEL>:<amount>) stays under Telegram's 64-byte limit.
#   m:<o>:<match_id>                 -> show selection buttons
#   p:<o>:<match_id>:<SEL>           -> show stake buttons
#   s:<o>:<match_id>:<SEL>:<amount>  -> place bet
#   c:<o>                            -> cancel / dismiss the flow

def cancel_button(owner_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("✖️ Cancel", callback_data=f"c:{owner_id}")


def matches_keyboard(matches, owner_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{m['home']} vs {m['away']}", callback_data=f"m:{owner_id}:{m['match_id']}"
        )]
        for m in matches[:10]
    ]
    return InlineKeyboardMarkup(rows)


def pick_keyboard(m, owner_id: int) -> InlineKeyboardMarkup:
    buttons = []
    for sel, odds in (("HOME", m["odds_home"]), ("DRAW", m["odds_draw"]), ("AWAY", m["odds_away"])):
        if odds is not None:
            buttons.append(
                InlineKeyboardButton(
                    f"{sel_label(m, sel)} @ {odds}",
                    callback_data=f"p:{owner_id}:{m['match_id']}:{sel}",
                )
            )
    return InlineKeyboardMarkup([[b] for b in buttons] + [[cancel_button(owner_id)]])


def stake_keyboard(match_id: str, selection: str, owner_id: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            money(a), callback_data=f"s:{owner_id}:{match_id}:{selection}:{a}"
        )
        for a in PRESET_STAKES
    ]
    allin = [InlineKeyboardButton(
        "🅰️ All-in", callback_data=f"s:{owner_id}:{match_id}:{selection}:all"
    )]
    return InlineKeyboardMarkup([row, allin, [cancel_button(owner_id)]])


# ---------------- shared bet placement ----------------

def place_bet(user_id: int, username: str, chat_id: int, match_id: str,
              selection: str, stake: float) -> tuple[bool, str]:
    """Validate and record a bet. Returns (ok, message_to_show)."""
    user = db.ensure_user(user_id, username, config.STARTING_BALANCE)

    m = db.get_match(match_id)
    if m is None:
        return False, "That match no longer exists. Check /matches."
    if m["status"] != "SCHEDULED" or m["kickoff"] <= db.now():
        return False, "Betting on that match is closed."

    odds = {"HOME": m["odds_home"], "DRAW": m["odds_draw"], "AWAY": m["odds_away"]}[selection]
    if odds is None:
        return False, "No odds available for that selection yet."
    if stake <= 0:
        return False, "Amount must be positive."
    if stake > user["balance"]:
        return False, f"Insufficient funds. Your balance is {money(user['balance'])}."

    db.adjust_balance(user_id, -stake)
    db.create_bet(user_id, match_id, selection, stake, odds, chat_id)
    return True, (
        f"✅ Bet placed: *{money(stake)}* on *{sel_label(m, selection)}* @ {odds}\n"
        f"{m['home']} vs {m['away']}\n"
        f"Potential return: *{money(stake * odds)}*  •  "
        f"Balance: {money(user['balance'] - stake)}"
    )


# ---------------- player commands ----------------

def commands_text() -> str:
    return (
        "*Commands*\n"
        "/matches – upcoming matches & odds\n"
        "/bet <code> <home|draw|away> <amount> – place a bet\n"
        "/mybets – your open bets\n"
        "/bets – everyone's bets on upcoming matches\n"
        "/history – your settled bet results\n"
        "/balance – your balance\n"
        "/leaderboard – top players\n"
        "/stats – all-time player betting stats\n"
        "/help – show this list"
    )


ADMIN_COMMANDS_TEXT = (
    "\n\n*Admin*\n"
    "/sync – fetch fixtures & odds from The Odds API\n"
    "/settle – force a settlement check now\n"
    "/result <code> <home>-<away> – record a score manually & settle\n"
    "/reset <@user|id> [amount] – set a player's balance\n"
    "/cancel <@user|bet id> – cancel a player's bet & refund"
)


def commands_block(user_id: int) -> str:
    """Player commands, plus the admin section when the user is an admin."""
    text = commands_text()
    if is_admin(user_id):
        text += ADMIN_COMMANDS_TEXT
    return text


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = reg(update)
    await update.message.reply_text(
        f"⚽ *{config.LEAGUE_NAME} Betting Bot*\n\n"
        f"Welcome, {user['username']}! You have {money(user['balance'])} to bet with.\n\n"
        + commands_block(update.effective_user.id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg(update)
    await update.message.reply_text(
        f"⚽ *{config.LEAGUE_NAME} Betting Bot*\n\n"
        + commands_block(update.effective_user.id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = reg(update)
    await update.message.reply_text(
        f"{user['username']}, your balance is *{money(user['balance'])}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: set a player's balance. Target by reply, @username, or id."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return

    args = ctx.args
    reply = update.message.reply_to_message
    if reply is not None:
        tu = reply.from_user
        target_id = tu.id
        target_name = tu.username or tu.first_name or str(tu.id)
        db.ensure_user(target_id, target_name, config.STARTING_BALANCE)
        amount_raw = args[0] if args else None
    else:
        if not args:
            await update.message.reply_text(
                "Usage: /reset <@username|user_id> [amount]\n"
                "Or reply to a player's message with /reset [amount].\n"
                f"Amount defaults to {money(config.STARTING_BALANCE)}."
            )
            return
        ref = args[0].lstrip("@")
        amount_raw = args[1] if len(args) > 1 else None
        user = db.get_user(int(ref)) if ref.isdigit() else db.get_user_by_username(ref)
        if user is None:
            await update.message.reply_text(
                "No such player (they must have used the bot first). "
                "Try replying to their message instead."
            )
            return
        target_id, target_name = user["user_id"], user["username"]

    if amount_raw is None:
        amount = float(config.STARTING_BALANCE)
    else:
        try:
            amount = float(amount_raw)
        except ValueError:
            await update.message.reply_text("Amount must be a number.")
            return
        if amount < 0:
            await update.message.reply_text("Amount can't be negative.")
            return

    db.set_balance(target_id, amount)
    await update.message.reply_text(
        f"🔄 Reset {html.escape(target_name)} to <b>{money(amount)}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_matches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg(update)
    matches = db.upcoming_matches(db.now())
    if not matches:
        await update.message.reply_text(
            "No upcoming matches loaded. An admin can run /sync to fetch them."
        )
        return

    lines = ["⚽ *Upcoming matches*\n"]
    for m in matches[:25]:
        odds = []
        if m["odds_home"] is not None:
            odds.append(f"1 {m['odds_home']}")
        if m["odds_draw"] is not None:
            odds.append(f"X {m['odds_draw']}")
        if m["odds_away"] is not None:
            odds.append(f"2 {m['odds_away']}")
        odds_str = "  ".join(odds) if odds else "odds TBD"
        lines.append(
            f"`{short(m['match_id'])}`  *{m['home']}* vs *{m['away']}*\n"
            f"   {kickoff_str(m['kickoff'])}\n"
            f"   {odds_str}"
        )
    lines.append("\nTap a match below to bet, or use `/bet <code> <home|draw|away> <amount>`")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=matches_keyboard(matches, update.effective_user.id),
    )


async def cmd_bet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg(update)
    args = ctx.args
    if len(args) != 3:
        await update.message.reply_text(
            "Usage: `/bet <code> <home|draw|away> <amount>`\n"
            "Example: `/bet 0d8a1f2b home 500`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code, sel_raw, amount_raw = args
    selection = SELECTIONS.get(sel_raw.lower())
    if selection is None:
        await update.message.reply_text(
            "Selection must be one of: home / draw / away (or 1 / x / 2)."
        )
        return

    try:
        stake = float(amount_raw)
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    found = db.resolve_match(code)
    if not found:
        await update.message.reply_text("No match with that code. Check /matches.")
        return
    if len(found) > 1:
        await update.message.reply_text(
            "That code matches several games — use more characters."
        )
        return

    ok, msg = place_bet(
        update.effective_user.id, display_name(update), update.effective_chat.id,
        found[0]["match_id"], selection, stake,
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_mybets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg(update)
    bets = db.open_bets_for_user_grouped(update.effective_user.id)
    if not bets:
        await update.message.reply_text("You have no open bets.")
        return
    lines = ["🎟️ *Your open bets*\n"]
    last_match = None
    for b in bets:
        if b["match_id"] != last_match:
            lines.append(f"*{b['home']}* vs *{b['away']}*")
            last_match = b["match_id"]
        pick = {"HOME": b["home"], "DRAW": "Draw", "AWAY": b["away"]}[b["selection"]]
        count = f" ({b['n_bets']} bets)" if b["n_bets"] > 1 else ""
        lines.append(
            f"   {money(b['stake'])} on {pick} @ {round(b['odds_at_bet'], 2)} "
            f"→ returns {money(b['potential'])}{count}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg(update)
    bets = db.settled_bets_for_user(update.effective_user.id)
    if not bets:
        await update.message.reply_text("No settled bets yet.")
        return
    lines = ["📜 *Your recent results*\n"]
    net = 0.0
    for b in bets:
        pick = {"HOME": b["home"], "DRAW": "Draw", "AWAY": b["away"]}[b["selection"]]
        change = b["payout"] - b["stake"]  # win: +profit, loss: -stake
        net += change
        outcome = (
            f"✅ +{money(change)}" if b["status"] == "WON" else f"❌ −{money(b['stake'])}"
        )
        lines.append(
            f"*{b['home']}* {b['home_score']}–{b['away_score']} *{b['away']}*\n"
            f"   {money(b['stake'])} on {pick} @ {round(b['odds_at_bet'], 2)} → {outcome}"
        )
    sign = "+" if net >= 0 else "−"
    lines.append(f"\n*Net (last {len(bets)}):* {sign}{money(abs(net))}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _book_block(m, book: dict, live: bool) -> str:
    """One match's per-outcome bet breakdown, as an HTML block."""
    when = "🔴 in play" if live else html.escape(kickoff_str(m["kickoff"]))
    header = (
        f"<b>{html.escape(m['home'])}</b> vs <b>{html.escape(m['away'])}</b> · {when}"
    )
    d = book.get(m["match_id"])
    if not d:
        return header + "\n   <i>no bets yet</i>"
    lines = [header]
    for sel in ("HOME", "DRAW", "AWAY"):
        entries = sorted(d[sel], key=lambda x: -x[1])
        label = html.escape(sel_label(m, sel))
        if entries:
            total = sum(a for _, a in entries)
            who = ", ".join(f"{html.escape(u)} {money(a)}" for u, a in entries)
            lines.append(f"   {label}: <b>{money(total)}</b> — {who}")
        else:
            lines.append(f"   {label}: —")
    return "\n".join(lines)


async def cmd_bets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show every player's open bets, grouped by outcome — live first, then upcoming."""
    reg(update)
    now = db.now()
    live = db.live_matches(now)
    upcoming = db.upcoming_matches(now)
    if not live and not upcoming:
        await update.message.reply_text(
            "No upcoming matches loaded. An admin can run /sync to fetch them."
        )
        return

    # match_id -> {SEL -> [(username, total_stake), ...]}
    book: dict = defaultdict(lambda: {"HOME": [], "DRAW": [], "AWAY": []})
    for r in db.open_bets_grouped():
        book[r["match_id"]][r["selection"]].append((r["username"], r["total"]))

    blocks = [_book_block(m, book, live=True) for m in live]
    blocks += [_book_block(m, book, live=False) for m in upcoming[:20]]

    await _reply_blocks(update, "💰 <b>Money on matches</b>", blocks)


async def _reply_blocks(update: Update, title: str, blocks: list[str]) -> None:
    """Send title + blocks as one or more HTML messages under Telegram's limit."""
    chunk = title
    for b in blocks:
        piece = "\n\n" + b
        if len(chunk) + len(piece) > 3500:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
            chunk = b
        else:
            chunk += piece
    if chunk:
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """All-time betting stats for every player who has placed a bet."""
    reg(update)
    rows = db.player_stats()
    if not rows:
        await update.message.reply_text("No bets placed yet.")
        return
    blocks = []
    for r in rows:
        lines = [
            f"<b>{html.escape(r['username'])}</b>",
            f"   Staked <b>{money(r['staked'])}</b> across {r['n_bets']} bets · "
            f"{r['matches']} matches · {r['outcomes']} outcomes",
        ]
        if r["won"] + r["lost"] > 0:
            sign = "+" if r["net"] >= 0 else "−"
            line = (
                f"   Record {r['won']}W–{r['lost']}L · "
                f"Net <b>{sign}{money(abs(r['net']))}</b>"
            )
            if r["biggest_win"] is not None:
                line += f" · Best +{money(r['biggest_win'])}"
            lines.append(line)
        blocks.append("\n".join(lines))
    await _reply_blocks(update, "📊 <b>Player stats</b>", blocks)


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reg(update)
    rows = db.leaderboard(10)
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Leaderboard</b> <i>(net worth)</i>\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        net = r["balance"] + r["staked"]
        line = f"{prefix} {html.escape(r['username'])} — <b>{money(net)}</b>"
        if r["staked"] > 0:
            line += f"  · {money(r['staked'])} in bets"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------- callback (inline button) flow ----------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kind, owner_raw, *rest = query.data.split(":")

    # Only the user who ran /matches may drive its buttons — otherwise another
    # member could hijack the shared message or place a bet by accident.
    if str(update.effective_user.id) != owner_raw:
        await query.answer("These buttons aren't yours — send /matches to bet.",
                           show_alert=True)
        return
    owner_id = int(owner_raw)
    reg(update)

    if kind == "m":  # match chosen -> show selections
        m = db.get_match(rest[0])
        if m is None:
            await query.answer("Match not found.", show_alert=True)
            return
        if m["status"] != "SCHEDULED" or m["kickoff"] <= db.now():
            await query.answer("Betting on that match is closed.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            f"*{m['home']}* vs *{m['away']}*\n{kickoff_str(m['kickoff'])}\n\nPick a result:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=pick_keyboard(m, owner_id),
        )

    elif kind == "p":  # selection chosen -> show stakes
        match_id, selection = rest[0], rest[1]
        m = db.get_match(match_id)
        if m is None:
            await query.answer("Match not found.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            f"*{m['home']}* vs *{m['away']}*\n"
            f"Your pick: *{sel_label(m, selection)}*\n\nHow much do you want to stake?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=stake_keyboard(match_id, selection, owner_id),
        )

    elif kind == "s":  # stake chosen -> place bet
        match_id, selection, amount_raw = rest[0], rest[1], rest[2]
        user = db.get_user(update.effective_user.id)
        stake = user["balance"] if amount_raw == "all" else float(amount_raw)
        ok, msg = place_bet(
            update.effective_user.id, display_name(update), update.effective_chat.id,
            match_id, selection, stake,
        )
        await query.answer("Bet placed!" if ok else "Couldn't place bet")
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)

    elif kind == "c":  # cancel / dismiss
        await query.answer("Cancelled")
        await query.edit_message_text("❌ Cancelled. Send /matches to bet again.")


# ---------------- admin commands ----------------

async def sync_matches() -> int:
    """Fetch fixtures & odds from The Odds API and upsert them. Returns count."""
    events = await odds_api.fetch_odds()
    for ev in events:
        db.upsert_match(
            ev["match_id"], ev["home"], ev["away"], ev["kickoff"],
            ev["odds_home"], ev["odds_draw"], ev["odds_away"],
        )
    return len(events)


async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return
    await update.message.reply_text("Fetching matches & odds…")
    try:
        n = await sync_matches()
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed")
        await update.message.reply_text(f"Sync failed: {e}")
        return
    await update.message.reply_text(f"✅ Synced {n} matches.")


async def sync_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        n = await sync_matches()
        log.info("auto-sync: %d matches", n)
    except Exception:  # noqa: BLE001
        log.exception("auto-sync failed")


async def cmd_settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return
    report: list[str] = []
    settled = await settle_finished_matches(ctx.application, force=True, report=report)
    msg = f"Settled {settled} match(es)." if settled else "Nothing to settle."
    if report:
        msg += "\n" + "\n".join(f"• {r}" for r in report)
    await update.message.reply_text(msg)


async def cmd_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: manually record a final score and settle the match.

    For when the scores API lags or a match has aged out of its lookback
    window. Usage: /result <code> <home_score>-<away_score>
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return

    usage = (
        "Usage: `/result <code> <home_score>-<away_score>`\n"
        "Example: `/result 0d8a1f2b 2-1`\n"
        "The first number is the *home* team's score. "
        "Records the result and settles all bets on the match."
    )
    args = ctx.args
    nums = [p for p in re.split(r"[-–: ]+", " ".join(args[1:]))] if len(args) >= 2 else []
    if len(nums) != 2 or not all(n.isdigit() for n in nums):
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)
        return
    home_score, away_score = int(nums[0]), int(nums[1])

    found = db.resolve_match(args[0])
    if not found:
        await update.message.reply_text("No match with that code.")
        return
    if len(found) > 1:
        await update.message.reply_text(
            "That code matches several games — use more characters."
        )
        return
    m = found[0]
    if m["status"] == "SETTLED":
        await update.message.reply_text("That match is already settled.")
        return
    if m["kickoff"] > db.now():
        await update.message.reply_text("That match hasn't kicked off yet.")
        return

    db.record_result(m["match_id"], home_score, away_score)
    settled = await settle_recorded_matches(ctx.application)
    winner = sel_label(m, _result(home_score, away_score))
    await update.message.reply_text(
        f"📝 Recorded {html.escape(m['home'])} <b>{home_score}–{away_score}</b> "
        f"{html.escape(m['away'])} (winner: <b>{html.escape(winner)}</b>). "
        f"Settled {settled} match(es).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: void a player's open bet and refund the stake.

    /cancel <@username|reply>  -> list that player's open bets with ids
    /cancel <bet_id>           -> cancel the bet and refund the player
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return

    args = ctx.args
    reply = update.message.reply_to_message

    # Resolve a target player (by reply or @username) -> list their open bets.
    target = None
    if reply is not None:
        target = db.get_user(reply.from_user.id)
    elif args and not args[0].lstrip("#").isdigit():
        target = db.get_user_by_username(args[0].lstrip("@"))

    if target is not None:
        bets = db.open_bets_for_user(target["user_id"])
        if not bets:
            await update.message.reply_text(
                f"{html.escape(target['username'])} has no open bets.",
                parse_mode=ParseMode.HTML,
            )
            return
        lines = [f"🎟️ <b>Open bets for {html.escape(target['username'])}</b>\n"]
        for b in bets:
            pick = {"HOME": b["home"], "DRAW": "Draw", "AWAY": b["away"]}[b["selection"]]
            lines.append(
                f"<code>#{b['bet_id']}</code>  {money(b['stake'])} on "
                f"{html.escape(pick)} @ {b['odds_at_bet']} — "
                f"{html.escape(b['home'])} vs {html.escape(b['away'])}"
            )
        lines.append("\nCancel one with /cancel &lt;bet id&gt;")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if not args or not args[0].lstrip("#").isdigit():
        await update.message.reply_text(
            "Usage: /cancel <@username> to list a player's open bets\n"
            "(or reply to their message with /cancel),\n"
            "then /cancel <bet id> to cancel one and refund the stake."
        )
        return

    bet = db.get_bet(int(args[0].lstrip("#")))
    if bet is None:
        await update.message.reply_text("No bet with that id.")
        return
    if bet["status"] != "OPEN":
        await update.message.reply_text(
            f"Bet #{bet['bet_id']} is already {bet['status']} and can't be cancelled."
        )
        return

    db.adjust_balance(bet["user_id"], bet["stake"])
    db.settle_bet(bet["bet_id"], "VOID", bet["stake"])
    pick = {"HOME": bet["home"], "DRAW": "Draw", "AWAY": bet["away"]}[bet["selection"]]
    await update.message.reply_text(
        f"🚫 Cancelled bet #{bet['bet_id']}: {html.escape(bet['username'])}'s "
        f"{money(bet['stake'])} on {html.escape(pick)} "
        f"({html.escape(bet['home'])} vs {html.escape(bet['away'])}).\n"
        f"Refunded <b>{money(bet['stake'])}</b>.",
        parse_mode=ParseMode.HTML,
    )


# ---------------- settlement ----------------

def _result(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "HOME"
    if home_score < away_score:
        return "AWAY"
    return "DRAW"


async def settle_finished_matches(
    app: Application, force: bool = False, report: list[str] | None = None
) -> int:
    """Pull recent scores, record results, pay out winning bets. Returns #settled.

    Skips the API call (and spends no credits) when no match is currently in a
    live window — see db.has_matches_in_play. `force` (manual /settle) bypasses
    that gate and looks back as far as the scores API allows, so matches that
    missed their live window (e.g. bot downtime) can still be settled.

    When `report` is given, appends a human-readable reason for every match
    that has kicked off but could not be settled.
    """
    def note(msg: str) -> None:
        if report is not None:
            report.append(msg)

    if not force and not db.has_matches_in_play(db.now()):
        return 0
    try:
        scores = await odds_api.fetch_scores(days_from=3 if force else 1)
    except Exception as e:  # noqa: BLE001
        log.exception("fetch_scores failed")
        note(f"score fetch failed: {e}")
        return 0

    # Record results for any tracked kicked-off match that has completed.
    pending = db.unsettled_past_matches(db.now())
    if not pending:
        note("no unsettled matches that have kicked off — is the match in the DB?")
    for m in pending:
        label = f"{short(m['match_id'])} {m['home']} vs {m['away']}"
        if m["status"] == "FINISHED":
            continue  # result already recorded; settled below
        info = scores.get(m["match_id"])
        if info is None:
            note(f"{label}: not in the scores API response (finished >3 days ago?)")
        elif not info["completed"]:
            note(f"{label}: API hasn't marked it completed yet")
        elif info["home_score"] is None:
            note(f"{label}: marked completed but no usable score returned")
        else:
            db.record_result(m["match_id"], info["home_score"], info["away_score"])

    return await settle_recorded_matches(app)


async def settle_recorded_matches(app: Application) -> int:
    """Pay out and announce every match with a recorded result. Returns #settled."""
    settled_count = 0
    for m in db.matches_to_settle():
        result = _result(m["home_score"], m["away_score"])
        # Settle every open bet; group the outcomes by the chat they were placed in.
        by_chat: dict[int, list] = {}
        for b in db.open_bets_for_match(m["match_id"]):
            won = b["selection"] == result
            payout = b["stake"] * b["odds_at_bet"] if won else 0
            db.adjust_balance(b["user_id"], payout)
            db.settle_bet(b["bet_id"], "WON" if won else "LOST", payout)
            chat = b["chat_id"] if b["chat_id"] is not None else _fallback_chat()
            by_chat.setdefault(chat, []).append(
                {"username": b["username"], "selection": b["selection"],
                 "stake": b["stake"], "won": won, "payout": payout}
            )
        db.mark_settled(m["match_id"])
        settled_count += 1
        await announce_result(app, m, result, by_chat)

    return settled_count


def _fallback_chat():
    """Where to announce bets that have no recorded chat (e.g. legacy bets)."""
    return next(iter(config.ADMIN_IDS), None)


async def announce_result(app, m, result, by_chat: dict):
    """Post the result with per-player wins and losses into each chat that bet."""
    header = (
        f"📣 <b>Result</b>\n"
        f"{html.escape(m['home'])} <b>{m['home_score']}–{m['away_score']}</b> "
        f"{html.escape(m['away'])}\n"
        f"Winner: <b>{html.escape(sel_label(m, result))}</b>\n"
    )
    for chat_id, entries in by_chat.items():
        if chat_id is None:
            continue
        lines = [header]
        winners = [e for e in entries if e["won"]]
        losers = [e for e in entries if not e["won"]]
        if winners:
            lines.append("\n<b>Winners</b>")
            for e in sorted(winners, key=lambda x: -(x["payout"] - x["stake"])):
                pick = html.escape(sel_label(m, e["selection"]))
                net = e["payout"] - e["stake"]  # profit, matching /history
                lines.append(
                    f"🎉 {html.escape(e['username'])}: {money(e['stake'])} on {pick} → "
                    f"<b>+{money(net)}</b>"
                )
        if losers:
            lines.append("\n<b>Losers</b>")
            for e in sorted(losers, key=lambda x: -x["stake"]):
                pick = html.escape(sel_label(m, e["selection"]))
                lines.append(
                    f"😢 {html.escape(e['username'])}: {money(e['stake'])} on {pick} → "
                    f"<b>−{money(e['stake'])}</b>"
                )
        try:
            await app.bot.send_message(
                chat_id, "\n".join(lines), parse_mode=ParseMode.HTML
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to announce result to chat %s", chat_id)


async def settle_job(ctx: ContextTypes.DEFAULT_TYPE):
    n = await settle_finished_matches(ctx.application)
    if n:
        log.info("settled %d matches", n)


# ---------------- bootstrap ----------------

def main():
    db.init()
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("matches", cmd_matches))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("mybets", cmd_mybets))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("bets", cmd_bets))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("settle", cmd_settle))
    app.add_handler(CommandHandler("result", cmd_result))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_repeating(
        settle_job, interval=config.SETTLE_INTERVAL, first=config.SETTLE_INTERVAL
    )
    # Refresh fixtures/odds automatically so each new gameweek's matches appear
    # without an admin having to run /sync. Also runs once ~10s after startup.
    app.job_queue.run_repeating(sync_job, interval=config.SYNC_INTERVAL, first=10)

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
