"""
Forest Study Group Bot — Cross-Batch Edition
---------------------------------------------
Tracks focus sessions across medical batches (Med22-Med27). Logging is
screenshot-only by design: forwarding a Forest completion screenshot gets
OCR'd for minutes + a lightweight date check, and each image's hash is
recorded so the same screenshot can never be logged twice. First-time
senders are auto-registered (batch chosen via button, no /register
needed). Managers can still `/log` on someone's behalf as a manual
correction by replying to their message.

Posts daily/weekly/monthly stats, per-batch leaderboards (both raw totals
and a fairer per-capita average), a permanent Hall of Fame, founder
badges, milestone shoutouts, and a daily "which tree did you plant" poll.
Two nominated "managers" post the day's flexible study window with
/setschedule, and can toggle a gentler /exammode during exam weeks.

Setup:
    1. pip install -r requirements.txt
    2. Set the BOT_TOKEN and GROUP_CHAT_ID environment variables
       (see README.md for how to get these)
    3. Run: python forest_bot.py

Requires tesseract-ocr to be installed on the host system for screenshot
reading (see README.md) — without it, logging is unavailable, since
screenshots are now the only way to log a session.
"""

import os
import sqlite3
import base64
import json
import logging
from datetime import datetime, date, timedelta, time as dtime
from io import BytesIO

try:
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import websockets

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Optional OCR support
try:
    from PIL import Image
    import pytesseract

    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")  # e.g. -1001234567890
DB_PATH = os.environ.get("DB_PATH", "forest.db")

# Telegram user IDs of the people allowed to set the daily study schedule.
# Comma-separated, e.g. "111111111,222222222". Get a user's ID by having
# them message @userinfobot.
MANAGER_IDS = {
    int(x) for x in os.environ.get("MANAGER_IDS", "").split(",") if x.strip().isdigit()
}

VALID_BATCHES = ["Med22", "Med23", "Med24", "Med25", "Med26", "Med27"]

# Optional: push a data.json snapshot to a GitHub repo so a static
# GitHub Pages site can display the leaderboard publicly. Leave any of
# these unset to disable website syncing entirely (the bot still works).
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "yourname/study-leaderboard"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_DATA_PATH = os.environ.get("GITHUB_DATA_PATH", "data.json")

# Official launch date (YYYY-MM-DD). Before this date, logging is blocked
# entirely — registration still works (early joiners still get founder
# badges), but no minutes can be claimed. This exists specifically so no
# one can screenshot old Forest history from before the competition
# existed and backdate it in. Leave unset to disable the gate (logging
# always allowed) — useful while testing before you've picked a date.
LAUNCH_DATE = os.environ.get("LAUNCH_DATE", "")  # e.g. "2026-10-05"


def is_before_launch() -> bool:
    if not LAUNCH_DATE:
        return False
    try:
        return local_today() < date.fromisoformat(LAUNCH_DATE)
    except ValueError:
        return False

# Timezone offset from UTC in hours, for scheduling posts at local time.
# Saudi Arabia is UTC+3. Change this if your group is elsewhere.
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))

MILESTONES_MINUTES = [600, 1500, 3000, 6000]  # 10h, 25h, 50h, 100h
STREAK_MILESTONES = [3, 7, 14, 30, 60]
STREAK_LABELS = {
    3: "🔥 3 أيام متتالية — بداية موفقة!",
    7: "🔥🔥 أسبوع كامل بدون انقطاع!",
    14: "🔥🔥 أسبوعين متتاليين — التزام حقيقي!",
    30: "🔥🔥🔥 شهر كامل من الاستمرارية — أسطوري!",
    60: "🔥🔥🔥 شهرين متتاليين — ما في وصف لهذا!",
}
MILESTONE_LABELS = {
    600: "🌱 10 ساعات تركيز — أول علامة فارقة!",
    1500: "🌿 25 ساعة تركيز — استمرار رائع!",
    3000: "🌳 50 ساعة تركيز — إنجاز كبير!",
    6000: "🌲 100 ساعة تركيز — أسطورة الغابة!",
}

# Level system — grows with the square root of all-time hours (fast early
# wins, naturally slower later, same shape as most XP curves). Titles are
# our own forest-growth naming, deliberately NOT a "Med Student / Clerk /
# Resident" copy of any other app's leaderboard wording — this one's ours.
LEVEL_TITLES = [
    (1, "🥉 برونزي"),
    (4, "🥈 فضي"),
    (8, "🥇 ذهبي"),
    (13, "💎 ماسي"),
    (19, "👑 أسطوري"),
]

# Bonus XP per day of an ongoing streak — rewards consistency, not just
# raw volume in one sitting (same idea as "streak bonuses included" on
# any XP-leaderboard app).
STREAK_XP_PER_DAY = 3


def level_for_total(total_minutes: int) -> int:
    hours = total_minutes / 60
    return 1 + int(hours ** 0.5)


def level_title_for(level: int) -> str:
    title = LEVEL_TITLES[0][1]
    for threshold, name in LEVEL_TITLES:
        if level >= threshold:
            title = name
    return title


def minutes_for_next_level(total_minutes: int) -> int:
    """Minutes still needed to reach the next level, for a 'so close!' nudge."""
    current_level = level_for_total(total_minutes)
    next_hours_needed = current_level ** 2  # inverse of the sqrt curve
    next_minutes_needed = next_hours_needed * 60
    return max(0, next_minutes_needed - total_minutes)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            minutes INTEGER NOT NULL,
            tag TEXT,
            logged_at TEXT NOT NULL,   -- ISO datetime (UTC)
            session_date TEXT NOT NULL, -- local date, YYYY-MM-DD
            source TEXT NOT NULL DEFAULT 'session' -- 'session' | 'daily_card' | 'manual'
        )
        """
    )
    # Lightweight migration for DBs created before the `source` column existed.
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'session'")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS milestones_hit (
            user_id INTEGER NOT NULL,
            milestone INTEGER NOT NULL,
            PRIMARY KEY (user_id, milestone)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS streak_milestones_hit (
            user_id INTEGER NOT NULL,
            milestone INTEGER NOT NULL,
            PRIMARY KEY (user_id, milestone)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS levels_hit (
            user_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            PRIMARY KEY (user_id, level)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            batch TEXT NOT NULL,
            founder INTEGER DEFAULT 0,
            registered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_of_fame (
            week_start TEXT PRIMARY KEY,
            user_id INTEGER,
            name TEXT,
            batch TEXT,
            minutes INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screenshots (
            hash TEXT PRIMARY KEY,
            user_id INTEGER,
            used_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def is_screenshot_used(hash_hex: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM screenshots WHERE hash=?", (hash_hex,)).fetchone()
    conn.close()
    return row is not None


def mark_screenshot_used(hash_hex: str, user_id: int):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO screenshots (hash, user_id, used_at) VALUES (?, ?, ?)",
        (hash_hex, user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


FOUNDER_SLOTS_PER_BATCH = 15


def batch_member_count(batch: str) -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE batch=?", (batch,)).fetchone()
    conn.close()
    return row["c"]


def get_setting(key: str, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def record_hall_of_fame(week_start: date, user_id: int, name: str, batch: str, minutes: int):
    conn = db()
    conn.execute(
        "INSERT INTO hall_of_fame (week_start, user_id, name, batch, minutes) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(week_start) DO UPDATE SET user_id=excluded.user_id, name=excluded.name, "
        "batch=excluded.batch, minutes=excluded.minutes",
        (week_start.isoformat(), user_id, name, batch, minutes),
    )
    conn.commit()
    conn.close()


def get_hall_of_fame(limit: int = 12):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM hall_of_fame ORDER BY week_start DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_user(user_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def register_user(user_id: int, display_name: str, batch: str) -> bool:
    """Returns True if this registration earned a founder badge (first
    FOUNDER_SLOTS_PER_BATCH people to register in that batch)."""
    is_founder = batch_member_count(batch) < FOUNDER_SLOTS_PER_BATCH
    conn = db()
    conn.execute(
        "INSERT INTO users (user_id, display_name, batch, founder, registered_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET batch=excluded.batch",
        (user_id, display_name, batch, int(is_founder), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return is_founder


def set_display_name(user_id: int, display_name: str):
    conn = db()
    conn.execute("UPDATE users SET display_name=? WHERE user_id=?", (display_name, user_id))
    conn.commit()
    conn.close()


def local_today() -> date:
    return (datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)).date()


def log_session(user_id: int, username: str, minutes: int, tag: str | None):
    """A single Forest-session entry (Timeline screenshot). Multiple of
    these on the same day ADD UP — unless a daily_card entry exists for
    that day, in which case the card's total wins (see _effective_cte)."""
    conn = db()
    conn.execute(
        "INSERT INTO sessions (user_id, username, minutes, tag, logged_at, session_date, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'session')",
        (
            user_id,
            username,
            minutes,
            tag,
            datetime.utcnow().isoformat(),
            local_today().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def log_daily_card(user_id: int, username: str, minutes: int, session_date: date | None = None):
    """A Forest 'Focus Statistics' daily-total screenshot. Replaces (never
    adds to) any earlier daily_card for the SAME user+day, since it's
    already a cumulative total for that day, not one more session.

    session_date defaults to today, but can be any past date — this is
    what makes catch-up logging possible: someone can grind all day, then
    send yesterday's (or any earlier day's) card at the end. Re-sending
    the same day's card later just revises that day; it never adds twice."""
    day_str = (session_date or local_today()).isoformat()
    conn = db()
    conn.execute(
        "DELETE FROM sessions WHERE user_id=? AND session_date=? AND source='daily_card'",
        (user_id, day_str),
    )
    conn.execute(
        "INSERT INTO sessions (user_id, username, minutes, tag, logged_at, session_date, source) "
        "VALUES (?, ?, ?, NULL, ?, ?, 'daily_card')",
        (user_id, username, minutes, datetime.utcnow().isoformat(), day_str),
    )
    conn.commit()
    conn.close()


# Reconciliation: for any user+day where a daily_card screenshot exists,
# that number is the truth for the day and per-session rows for that same
# day are ignored (they'd otherwise double-count what the card already
# totals). Days with no daily_card just sum their session rows as before.
_EFFECTIVE_CTE = """
    WITH daily_effective AS (
        SELECT user_id, session_date,
            COALESCE(
                MAX(CASE WHEN source='daily_card' THEN minutes END),
                SUM(CASE WHEN source!='daily_card' THEN minutes ELSE 0 END)
            ) AS minutes
        FROM sessions
        WHERE session_date >= :since
        GROUP BY user_id, session_date
    )
"""


def total_minutes(user_id: int) -> int:
    conn = db()
    row = conn.execute(
        _EFFECTIVE_CTE
        + "SELECT COALESCE(SUM(minutes),0) AS m FROM daily_effective WHERE user_id=:uid",
        {"since": "2000-01-01", "uid": user_id},
    ).fetchone()
    conn.close()
    return row["m"]


def leaderboard(since_date: date, limit: int = 10, batch: str | None = None):
    conn = db()
    query = (
        _EFFECTIVE_CTE
        + """
        SELECT de.user_id,
               COALESCE(u.display_name, de.user_id) AS name,
               COALESCE(u.batch, '') AS batch,
               SUM(de.minutes) AS total
        FROM daily_effective de
        LEFT JOIN users u ON u.user_id = de.user_id
        """
    )
    params = {"since": since_date.isoformat()}
    if batch:
        query += " WHERE u.batch = :batch"
        params["batch"] = batch
    query += " GROUP BY de.user_id ORDER BY total DESC LIMIT :limit"
    params["limit"] = limit
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def batches_with_activity(since_date: date):
    conn = db()
    rows = conn.execute(
        """
        SELECT DISTINCT u.batch FROM sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.session_date >= ? AND u.batch IS NOT NULL
        """,
        (since_date.isoformat(),),
    ).fetchall()
    conn.close()
    return [r["batch"] for r in rows]


def most_improved(this_week_start: date, last_week_start: date):
    """Return the user with the largest increase vs the prior week."""
    conn = db()
    this_week = {
        r["user_id"]: r["total"]
        for r in conn.execute(
            _EFFECTIVE_CTE
            + "SELECT user_id, SUM(minutes) AS total FROM daily_effective GROUP BY user_id",
            {"since": this_week_start.isoformat()},
        ).fetchall()
    }
    last_week_query = """
        WITH daily_effective AS (
            SELECT user_id, session_date,
                COALESCE(
                    MAX(CASE WHEN source='daily_card' THEN minutes END),
                    SUM(CASE WHEN source!='daily_card' THEN minutes ELSE 0 END)
                ) AS minutes
            FROM sessions
            WHERE session_date >= :since AND session_date < :until
            GROUP BY user_id, session_date
        )
        SELECT user_id, SUM(minutes) AS total FROM daily_effective GROUP BY user_id
    """
    last_week = {
        r["user_id"]: r["total"]
        for r in conn.execute(
            last_week_query,
            {"since": last_week_start.isoformat(), "until": this_week_start.isoformat()},
        ).fetchall()
    }
    names = {
        r["user_id"]: r["name"]
        for r in conn.execute(
            "SELECT s.user_id AS user_id, COALESCE(u.display_name, s.username) AS name "
            "FROM sessions s LEFT JOIN users u ON u.user_id = s.user_id "
            "WHERE s.session_date >= ? GROUP BY s.user_id",
            (last_week_start.isoformat(),),
        ).fetchall()
    }
    conn.close()
    best_user, best_delta = None, 0
    for uid, total in this_week.items():
        delta = total - last_week.get(uid, 0)
        if delta > best_delta:
            best_delta, best_user = delta, names.get(uid, str(uid))
    return best_user, best_delta


def current_streak(user_id: int) -> int:
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT session_date FROM sessions WHERE user_id=? ORDER BY session_date DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    dates = {date.fromisoformat(r["session_date"]) for r in rows}
    streak = 0
    cursor = local_today()
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def get_top_streaks(limit: int = 8):
    """Top current streaks across all registered users. O(n) over the
    user list — fine at this scale, revisit if it ever gets huge."""
    conn = db()
    users = conn.execute("SELECT user_id, display_name, batch FROM users").fetchall()
    conn.close()
    results = []
    for u in users:
        s = current_streak(u["user_id"])
        if s > 0:
            results.append({"name": u["display_name"], "batch": u["batch"], "streak": s})
    results.sort(key=lambda r: r["streak"], reverse=True)
    return results[:limit]


async def check_and_announce_streak_milestones(update, context, user):
    streak = current_streak(user.id)
    conn = db()
    for m in STREAK_MILESTONES:
        if streak >= m:
            already = conn.execute(
                "SELECT 1 FROM streak_milestones_hit WHERE user_id=? AND milestone=?",
                (user.id, m),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO streak_milestones_hit (user_id, milestone) VALUES (?, ?)",
                    (user.id, m),
                )
                conn.commit()
                if GROUP_CHAT_ID:
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"{STREAK_LABELS[m]}\n{user.first_name} مستمر بدون انقطاع!",
                    )
    conn.close()


async def check_and_announce_level_up(update, context, user):
    total = total_minutes(user.id)
    level = level_for_total(total)
    if level <= 1:
        return
    conn = db()
    already = conn.execute(
        "SELECT 1 FROM levels_hit WHERE user_id=? AND level=?", (user.id, level)
    ).fetchone()
    if not already:
        conn.execute("INSERT INTO levels_hit (user_id, level) VALUES (?, ?)", (user.id, level))
        conn.commit()
        title = level_title_for(level)
        if GROUP_CHAT_ID:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⭐ ترقية! {user.first_name} صار بالمستوى {level} — {title}",
            )
    conn.close()


def _rows_to_list(rows):
    out = []
    for r in rows:
        conn = db()
        founder_row = conn.execute(
            "SELECT founder FROM users WHERE user_id=?", (r["user_id"],)
        ).fetchone()
        conn.close()
        all_time = total_minutes(r["user_id"])  # level reflects career-to-date, not just this period
        level = level_for_total(all_time)
        streak_bonus = current_streak(r["user_id"]) * STREAK_XP_PER_DAY
        out.append({
            "name": r["name"],
            "batch": r["batch"] or None,
            "minutes": r["total"],
            "founder": bool(founder_row and founder_row["founder"]),
            "level": level,
            "level_title": level_title_for(level),
            "xp": r["total"] + streak_bonus,
        })
    return out


def batch_totals(since_date: date) -> dict:
    """Raw sum per batch — deliberately kept alongside the per-capita average:
    a batch can climb this one just by recruiting more people, which is the
    point (it's the growth/recruitment incentive)."""
    conn = db()
    rows = conn.execute(
        _EFFECTIVE_CTE
        + """
        SELECT u.batch, SUM(de.minutes) AS total
        FROM daily_effective de JOIN users u ON u.user_id = de.user_id
        GROUP BY u.batch
        """,
        {"since": since_date.isoformat()},
    ).fetchall()
    conn.close()
    return {r["batch"]: r["total"] for r in rows}


def batch_averages(since_date: date):
    """Average minutes per registered member, per batch — fairer than a raw
    sum since batches don't all have the same number of people signed up."""
    totals = batch_totals(since_date)
    result = {}
    for batch, total in totals.items():
        count = batch_member_count(batch)
        if count:
            result[batch] = round(total / count, 1)
    return result


def build_export_data() -> dict:
    today = local_today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    epoch = date(2000, 1, 1)

    weekly_by_batch = {}
    for batch in VALID_BATCHES:
        rows = leaderboard(week_start, limit=50, batch=batch)
        if rows:
            weekly_by_batch[batch] = _rows_to_list(rows)

    last_week_start = week_start - timedelta(days=7)
    improved_name, improved_delta = most_improved(week_start, last_week_start)

    hof_rows = get_hall_of_fame(limit=12)
    hall_of_fame = [
        {"week_start": r["week_start"], "name": r["name"], "batch": r["batch"], "minutes": r["minutes"]}
        for r in hof_rows
    ]

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "exam_mode": get_setting("exam_mode", "off") == "on",
        "daily": _rows_to_list(leaderboard(today, limit=50)),
        "weekly": _rows_to_list(leaderboard(week_start, limit=50)),
        "monthly": _rows_to_list(leaderboard(month_start, limit=50)),
        "all_time": _rows_to_list(leaderboard(epoch, limit=50)),
        "weekly_by_batch": weekly_by_batch,
        "weekly_batch_averages": batch_averages(week_start),
        "weekly_batch_totals": batch_totals(week_start),
        "hall_of_fame": hall_of_fame,
        "most_improved": {"name": improved_name, "delta": improved_delta} if improved_name else None,
        "top_streaks": get_top_streaks(limit=8),
    }


CONNECTED_CLIENTS: set = set()


async def broadcast_update():
    """Pushes the current leaderboard to every browser tab with the site
    open, over the WebSocket server started in main(). This is what makes
    the site update live instead of only on page refresh. Safe to call
    even if no server is running / no clients are connected."""
    if not CONNECTED_CLIENTS:
        return
    try:
        payload = json.dumps(build_export_data(), ensure_ascii=False)
    except Exception:
        logger.exception("Failed to build export data for broadcast")
        return
    dead = set()
    for ws in CONNECTED_CLIENTS:
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    CONNECTED_CLIENTS.difference_update(dead)


async def ws_handler(websocket):
    """One entry per connected browser tab. Sends the current leaderboard
    immediately on connect, then just keeps the socket open — all further
    updates come from broadcast_update() being called elsewhere whenever
    someone logs a session."""
    CONNECTED_CLIENTS.add(websocket)
    try:
        payload = json.dumps(build_export_data(), ensure_ascii=False)
        await websocket.send(payload)
        async for _ in websocket:
            pass  # the site never sends us anything; just keep the connection open
    except Exception:
        pass
    finally:
        CONNECTED_CLIENTS.discard(websocket)


async def start_ws_server(app):
    """Runs the live-update WebSocket server on Railway's assigned $PORT,
    alongside the bot's own polling loop, for as long as the app runs."""
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("websockets package not installed — live updates disabled")
        return
    port = int(os.environ.get("PORT", 8765))
    server = await websockets.serve(ws_handler, "0.0.0.0", port)
    app.bot_data["ws_server"] = server
    logger.info(f"Live-update WebSocket server listening on 0.0.0.0:{port}")


def push_leaderboard_to_github():
    """Best-effort push of the current leaderboard to a GitHub repo file,
    so a static GitHub Pages site can read it. Silently no-ops if the
    GitHub env vars aren't set, or if the `requests` package is missing."""
    if not (REQUESTS_AVAILABLE and GITHUB_TOKEN and GITHUB_REPO):
        return

    data = build_export_data()
    content_str = json.dumps(data, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_DATA_PATH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    sha = None
    try:
        resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=10)
        if resp.status_code == 200:
            sha = resp.json().get("sha")
    except Exception:
        logger.exception("Could not fetch existing data.json sha from GitHub")

    payload = {
        "message": "Update leaderboard data",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        put_resp = requests.put(api_url, headers=headers, json=payload, timeout=10)
        if put_resp.status_code not in (200, 201):
            logger.warning("GitHub push failed (%s): %s", put_resp.status_code, put_resp.text)
    except Exception:
        logger.exception("Failed to push leaderboard data to GitHub")


def daily_total_all() -> int:
    today_str = local_today().isoformat()
    conn = db()
    row = conn.execute(
        _EFFECTIVE_CTE + "SELECT COALESCE(SUM(minutes),0) AS m FROM daily_effective",
        {"since": today_str},
    ).fetchone()
    conn.close()
    return row["m"]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def default_display_name(user) -> str:
    """Leaderboards show Telegram @usernames, not real names — a deliberate
    privacy/tone choice, not a fallback. Only when someone has no public
    Telegram username do we fall back to their Telegram first name, and
    even then /setname lets them pick a handle-style name instead."""
    if user.username:
        return f"@{user.username}"
    return user.first_name


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/register Med25"""
    args = context.args
    user = update.effective_user
    if not args or args[0] not in VALID_BATCHES:
        await update.message.reply_text(
            "استخدم: /register Med25\n"
            f"الدفعات المتاحة: {', '.join(VALID_BATCHES)}"
        )
        return
    batch = args[0]
    is_founder = register_user(user.id, default_display_name(user), batch)
    founder_line = (
        f"\n🏅 أنت من أوائل المؤسسين في {batch} — راح يظهر وسمك دايمًا!"
        if is_founder
        else ""
    )
    # Each on its own line, with an LRM before the @handle, so Telegram's
    # Arabic (RTL) rendering doesn't flip the @ to the wrong side of the
    # username — putting an LTR token mid-sentence in RTL text does that.
    await update.message.reply_text(
        f"✅ تم تسجيلك يا {user.first_name} ضمن دفعة {batch}!{founder_line}\n\n"
        f"📛 بالمتصدرين بيظهر اسمك كذا: \u200e{default_display_name(user)}\n"
        "تبي تغيّره لأي اسم ثاني؟ أرسل: /setname الاسم_الي_تبيه"
    )


async def cmd_setname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Overrides the leaderboard name — mainly for people with no Telegram
    @username, or who want a different handle shown than their real one."""
    args = context.args
    user = update.effective_user
    if not get_user(user.id):
        await update.message.reply_text("سجّل نفسك أولًا بالأمر: /register Med25")
        return
    if not args:
        await update.message.reply_text("استخدم: /setname اليوزرنيم أو اللقب الي تبيه يظهر بالمتصدرين")
        return
    name = " ".join(args)
    set_display_name(user.id, name)
    await update.message.reply_text(f"✅ تم تحديث اسمك بالمتصدرين إلى: {name}")


async def cmd_setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setschedule 13:00 22:00 — restricted to managers"""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص لمنظّمي الجدول فقط.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("استخدم: /setschedule 13:00 22:00")
        return
    start, end = args
    text = f"📅 جدول اليوم:\n⏰ من {start} إلى {end}\nيلا بينا نزرع! 🌱"
    if GROUP_CHAT_ID:
        msg = await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
        try:
            await context.bot.pin_chat_message(chat_id=GROUP_CHAT_ID, message_id=msg.message_id)
        except Exception:
            pass
    else:
        await update.message.reply_text(text)


async def cmd_exammode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/exammode on|off — managers only. Softens the vibe during exam weeks:
    the website shows a banner and milestone posts pause their noise."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص لمنظّمي الجدول فقط.")
        return
    args = context.args
    if not args or args[0] not in ("on", "off"):
        await update.message.reply_text("استخدم: /exammode on أو /exammode off")
        return
    set_setting("exam_mode", args[0])
    if args[0] == "on":
        await update.message.reply_text(
            "📚 وضع الاختبارات مفعّل. خذوا وقتكم، والمذاكرة أهم من الترتيب هالفترة. بالتوفيق!"
        )
    else:
        await update.message.reply_text("✅ رجعنا للوضع العادي — يلا نكمل المنافسة!")


async def cmd_findpartner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/findpartner رياضيات — finds someone else who logged the same tag recently."""
    user = update.effective_user
    if not get_user(user.id):
        await update.message.reply_text("سجّل نفسك أولًا: /register Med25")
        return
    if not context.args:
        await update.message.reply_text("استخدم: /findpartner اسم المادة")
        return
    tag = " ".join(context.args)
    cutoff = (local_today() - timedelta(days=7)).isoformat()
    conn = db()
    row = conn.execute(
        """
        SELECT DISTINCT s.user_id, COALESCE(u.display_name, s.username) AS name
        FROM sessions s
        LEFT JOIN users u ON u.user_id = s.user_id
        WHERE s.tag = ? AND s.session_date >= ? AND s.user_id != ?
        LIMIT 1
        """,
        (tag, cutoff, user.id),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(
            f"ما لقيت أحد يذاكر «{tag}» هالأسبوع. جرب لاحقًا أو غيّر المادة."
        )
        return
    await update.message.reply_text(
        f"🤝 لقيت لك رفيق مذاكرة لمادة «{tag}»: {row['name']}\n"
        "راسله وابدأوا جلسة Plant Together!"
    )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager-only manual correction: reply to the student's message with
    /log 90 to add/fix an entry. Regular logging is screenshot-only — see
    handle_photo below."""
    user = update.effective_user
    if not MANAGER_IDS or user.id not in MANAGER_IDS:
        await update.message.reply_text(
            "تسجيل الجلسات صار بالصورة فقط — صوّر شاشة اكتمال الجلسة في Forest وابعثها هنا 📸"
        )
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("رد على رسالة الطالب بهذا الأمر: /log 90")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("استخدم: /log 90 (وأنت رادّ على رسالة الطالب)")
        return

    target = update.message.reply_to_message.from_user
    minutes = int(args[0])
    if not get_user(target.id):
        await update.message.reply_text("هذا الشخص ما سجّل نفسه بعد.")
        return

    log_session(target.id, target.username or target.full_name, minutes, "manager-correction")
    push_leaderboard_to_github()
    await broadcast_update()
    await update.message.reply_text(
        f"✅ تم تسجيل {minutes} دقيقة يدويًا لـ {target.first_name} (تصحيح من المنظم)."
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    total = total_minutes(user.id)
    streak = current_streak(user.id)
    level = level_for_total(total)
    title = level_title_for(level)
    remaining = minutes_for_next_level(total)
    xp = total + streak * STREAK_XP_PER_DAY
    await update.message.reply_text(
        f"📊 إحصائياتك يا {user.first_name}:\n"
        f"— المستوى {level} · {title}\n"
        f"— الإجمالي: {total} دقيقة ({total // 60} ساعة) — {xp} XP\n"

        f"— التتابع الحالي: {streak} يوم\n"
        f"— باقي {remaining} دقيقة للمستوى {level + 1}!"
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/leaderboard or /leaderboard Med25 for a single batch"""
    week_start = local_today() - timedelta(days=local_today().weekday())
    batch_filter = context.args[0] if context.args else None
    rows = leaderboard(week_start, batch=batch_filter)
    if not rows:
        await update.message.reply_text("لا توجد جلسات مسجّلة هذا الأسبوع بعد.")
        return
    medals = ["🥇", "🥈", "🥉"]
    title = f"🏆 ترتيب هذا الأسبوع"
    title += f" — {batch_filter}" if batch_filter else " (كل الدفعات)"
    lines = [title + "\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        batch_tag = f" [{r['batch']}]" if not batch_filter and r["batch"] else ""
        lines.append(f"{prefix} {r['name']}{batch_tag} — {r['total']} دقيقة")
    await update.message.reply_text("\n".join(lines))


async def check_and_announce_milestones(update, context, user):
    total = total_minutes(user.id)
    conn = db()
    for m in MILESTONES_MINUTES:
        if total >= m:
            already = conn.execute(
                "SELECT 1 FROM milestones_hit WHERE user_id=? AND milestone=?",
                (user.id, m),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO milestones_hit (user_id, milestone) VALUES (?, ?)",
                    (user.id, m),
                )
                conn.commit()
                if GROUP_CHAT_ID:
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"🎉 مبروك لـ {user.first_name}!\n{MILESTONE_LABELS[m]}",
                    )
    conn.close()


# ---------------------------------------------------------------------------
# Screenshot (OCR) handler
# ---------------------------------------------------------------------------


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def extract_minutes_from_ocr(text: str) -> int | None:
    """Parses a single Timeline entry's duration. Priority matters here:
    Forest's own phrasing ('75-minute Apple Tree') is unambiguous, so it's
    checked first. The MM:SS pattern is checked last and deliberately NOT
    first, because a completed entry also shows a start-end clock range
    like '07:12 - 08:28' that would otherwise be misread as the duration."""
    import re

    match = re.search(r"(\d{1,3})[\s-]*minutes?\b", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # A running/paused timer display, e.g. "90:00"
    match = re.search(r"\b(\d{1,3}):00\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{2,3})\b", text)
    if match:
        return int(match.group(1))
    return None


def is_daily_card_screenshot(text: str) -> bool:
    """True for Forest's 'Focus Statistics' daily-overview card (the one
    with a Share button and a stamped date like '09.27 2026'), as opposed
    to a single Timeline session entry."""
    import re

    t = text.lower()
    if "focused time" in t or "focus statistics" in t or "focus trend" in t:
        return True
    return bool(re.search(r"\d{2}\.\d{2}\s*20\d{2}", text))


def extract_daily_card_minutes(text: str) -> int | None:
    """Parses totals like '1 hour 25 minutes' or '1.4 hours' off the daily card."""
    import re

    m = re.search(r"(\d+)\s*hours?\s+(\d+)\s*minutes?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*hours?", text, re.IGNORECASE)
    if m:
        return round(float(m.group(1)) * 60)
    m = re.search(r"(\d{1,3})\s*minutes?", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def extract_card_date(text: str) -> date | None:
    """Parses the daily card's stamped date. Unlike the Timeline heuristic
    below, this is precise enough to use as the actual session_date —
    which is what makes retroactive catch-up logging safe: the card names
    its own day, so the bot never has to assume 'today'.

    Forest shows the date in at least two different formats depending on
    the screen/version — '09.27 2026' (dotted numeric) on the Focus
    Statistics card, and 'Oct 2, 2026 (Today)' (month name) on the
    Overview screen — so both are tried."""
    import re

    m = re.search(r"(\d{2})\.(\d{2})\s*(20\d{2})", text)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            pass

    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})", text)
    if m:
        month = _MONTH_NAMES.get(m.group(1)[:3].lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                return None

    return None


def check_screenshot_date(text: str, today: date) -> str:
    """Weak heuristic, not proof: looks for today's day-of-month next to
    today's month abbreviation. Returns 'match', 'mismatch' (a different
    month abbreviation was found, suggesting an old/reused screenshot), or
    'unknown' (couldn't find a date at all — Forest's screen may not show
    one, so this never hard-blocks on its own)."""
    import re

    this_month = MONTH_ABBR[today.month - 1]
    day = str(today.day)
    if re.search(rf"{this_month}\D{{0,3}}{day}\b", text) or re.search(
        rf"\b{day}\D{{0,3}}{this_month}", text
    ):
        return "match"
    for abbr in MONTH_ABBR:
        if abbr != this_month and re.search(abbr, text):
            return "mismatch"
    return "unknown"


async def _ocr_and_prepare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads the photo, runs OCR + anti-cheat checks, and returns
    (minutes, image_hash, source, session_date) on success — source is
    'daily_card' for Forest's Focus Statistics screen (date-stamped, one
    per day, replaces that day's total — can be a PAST day, which is how
    catch-up logging works) or 'session' for a single Timeline entry
    (always logged under today). Sends an error reply and returns None on
    failure."""
    import hashlib

    if is_before_launch():
        await update.message.reply_text(
            f"📚 التسجيل الرسمي يبدأ {LAUNCH_DATE} — سجّل دفعتك الحين لأخذ وسم المؤسس 🏅، "
            "وارجع تصوّر جلساتك من يوم الإطلاق."
        )
        return None

    if not OCR_AVAILABLE:
        await update.message.reply_text(
            "قراءة الصور غير مفعّلة على هذا الخادم حاليًا — تواصل مع أحد المنظمين."
        )
        return None

    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = BytesIO()
    await file.download_to_memory(buf)
    raw = buf.getvalue()
    image_hash = hashlib.sha256(raw).hexdigest()

    if is_screenshot_used(image_hash):
        await update.message.reply_text(
            "⚠️ هذي الصورة مسجّلة من قبل — لازم صورة جديدة لكل تسجيل."
        )
        return None

    buf.seek(0)
    try:
        img = Image.open(buf)
        text = pytesseract.image_to_string(img)
    except Exception:
        # Logged server-side (visible in Railway's Deploy Logs) so a real
        # failure — e.g. the Tesseract engine missing — is distinguishable
        # from an actually blurry photo, instead of both looking identical
        # to the user.
        logger.exception("OCR failed while reading a submitted screenshot")
        await update.message.reply_text("ما قدرت أقرأ الصورة، صوّر شاشة أوضح وجرّب مرة ثانية.")
        return None

    today = local_today()

    if is_daily_card_screenshot(text):
        minutes = extract_daily_card_minutes(text)
        if minutes is None:
            await update.message.reply_text(
                "لقيت بطاقة الإحصائيات اليومية بس ما قدرت أقرأ الوقت الإجمالي بوضوح."
            )
            return None

        card_date = extract_card_date(text)
        if card_date is None:
            await update.message.reply_text(
                "ما قدرت أقرأ تاريخ البطاقة بوضوح. تأكد إن التاريخ أعلى البطاقة ظاهر بالصورة."
            )
            return None
        if card_date > today:
            await update.message.reply_text("⚠️ هذا تاريخ بالمستقبل! تأكد إنك صوّرت اليوم الصحيح.")
            return None
        if LAUNCH_DATE and card_date < date.fromisoformat(LAUNCH_DATE):
            await update.message.reply_text(
                f"⚠️ هذا التاريخ قبل بداية المسابقة الرسمية ({LAUNCH_DATE}) — ما يُحتسب."
            )
            return None

        return minutes, image_hash, "daily_card", card_date

    minutes = extract_minutes_from_ocr(text)
    if minutes is None:
        await update.message.reply_text(
            "ما لقيت رقم واضح بالصورة. تأكد إن شاشة اكتمال الجلسة أو بطاقة اليوم كاملة وواضحة."
        )
        return None

    date_status = check_screenshot_date(text, today)
    if date_status == "mismatch":
        await update.message.reply_text(
            "⚠️ يبدو إن تاريخ الصورة مو تاريخ اليوم. جلسات Timeline تُسجَّل لليوم الحالي فقط — "
            "لو تبي تسجّل يوم فات، استخدم بطاقة ذاك اليوم من Overview بدالها."
        )
        return None

    return minutes, image_hash, "session", None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Logging only happens in DM with the bot — keeps the group feed clean
    # (announcements, polls, milestones) instead of filling up with photo
    # confirmations. Redirect politely rather than silently ignoring it.
    if update.effective_chat.type != "private":
        bot_username = context.bot.username
        await update.message.reply_text(
            f"📩 سجّل من الخاص مع البوت مباشرة (@{bot_username})، مو هنا بالقروب — "
            "أبعث له الصورة هناك وبيردّ عليك."
        )
        return

    result = await _ocr_and_prepare(update, context)
    if result is None:
        return
    minutes, image_hash, source, session_date = result
    kind_label = "📊 بطاقة يوم" if source == "daily_card" else "📸 جلسة واحدة"
    day_note = ""
    if source == "daily_card" and session_date != local_today():
        day_note = f" (ليوم {session_date.isoformat()})"

    if not get_user(user.id):
        # Auto-registration: hold the OCR'd data and ask which batch they're in.
        context.user_data["pending_minutes"] = minutes
        context.user_data["pending_hash"] = image_hash
        context.user_data["pending_source"] = source
        context.user_data["pending_date"] = session_date.isoformat() if session_date else None
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(b, callback_data=f"regbatch:{b}")] for b in VALID_BATCHES]
        )
        await update.message.reply_text(
            f"{kind_label}{day_note} — وجدت {minutes} دقيقة 👍\nقبل لا نسجّلها، وش دفعتك؟",
            reply_markup=keyboard,
        )
        return

    context.user_data["pending_minutes"] = minutes
    context.user_data["pending_hash"] = image_hash
    context.user_data["pending_source"] = source
    context.user_data["pending_date"] = session_date.isoformat() if session_date else None
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:{source}:{minutes}"),
                InlineKeyboardButton("✏️ مو صحيح", callback_data="edit"),
            ]
        ]
    )
    note = (
        "\n(بطاقة اليوم تحل محل أي جلسات سجّلتها لنفس اليوم، مو تضاف عليها)"
        if source == "daily_card"
        else ""
    )
    await update.message.reply_text(
        f"{kind_label}{day_note} — وجدت {minutes} دقيقة — تأكيد؟{note}", reply_markup=keyboard
    )


async def _finalize_log(
    update, context, user, minutes: int, image_hash: str | None, source: str, session_date: date | None
):
    if source == "daily_card":
        log_daily_card(user.id, user.username or user.full_name, minutes, session_date)
    else:
        log_session(user.id, user.username or user.full_name, minutes, None)
    if image_hash:
        mark_screenshot_used(image_hash, user.id)
    await check_and_announce_milestones(update, context, user)
    await check_and_announce_streak_milestones(update, context, user)
    await check_and_announce_level_up(update, context, user)
    push_leaderboard_to_github()
    await broadcast_update()
    total = total_minutes(user.id)
    streak = current_streak(user.id)
    await update.callback_query.edit_message_text(
        f"✅ تم تسجيل {minutes} دقيقة! إجمالي رصيدك: {total} دقيقة (Lv{level_for_total(total)} · {level_title_for(level_for_total(total))}) — 🔥 {streak} يوم متتالي"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    if query.data.startswith("regbatch:"):
        batch = query.data.split(":", 1)[1]
        is_founder = register_user(user.id, default_display_name(user), batch)
        minutes = context.user_data.get("pending_minutes")
        source = context.user_data.get("pending_source", "session")
        founder_line = f" 🏅 وأنت من أوائل مؤسسي {batch}!" if is_founder else ""
        setname_hint = " (تقدر تغيّر اسمك بالمتصدرين بالأمر /setname)"
        if minutes is None:
            await query.edit_message_text(f"✅ تم تسجيلك ضمن {batch}!{founder_line}{setname_hint}")
            return
        await query.edit_message_text(
            f"✅ تم تسجيلك ضمن {batch}!{founder_line}{setname_hint}\nوجدت {minutes} دقيقة بالصورة — تأكيد؟",
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="اضغط للتأكيد:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:{source}:{minutes}"),
                        InlineKeyboardButton("✏️ مو صحيح", callback_data="edit"),
                    ]
                ]
            ),
        )

    elif query.data.startswith("confirm:"):
        _, source, minutes_str = query.data.split(":", 2)
        minutes = int(minutes_str)
        image_hash = context.user_data.get("pending_hash")
        date_str = context.user_data.get("pending_date")
        session_date = date.fromisoformat(date_str) if date_str else None
        if image_hash and is_screenshot_used(image_hash):
            await query.edit_message_text("⚠️ هذي الصورة اتسجّلت بالفعل.")
            return
        await _finalize_log(update, context, user, minutes, image_hash, source, session_date)
        context.user_data.pop("pending_minutes", None)
        context.user_data.pop("pending_hash", None)
        context.user_data.pop("pending_source", None)
        context.user_data.pop("pending_date", None)

    elif query.data == "edit":
        await query.edit_message_text(
            "تمام، صوّر شاشة أوضح — إما جلسة واحدة أو بطاقة اليوم الكاملة — وجرّب من جديد."
        )
        context.user_data.pop("pending_minutes", None)
        context.user_data.pop("pending_hash", None)
        context.user_data.pop("pending_source", None)
        context.user_data.pop("pending_date", None)


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


async def job_sync_website(context: ContextTypes.DEFAULT_TYPE):
    """Fallback safety-net sync, in case a per-log push ever fails silently."""
    push_leaderboard_to_github()
    await broadcast_update()


async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    total = daily_total_all()
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=f"🌲 دقائق التركيز اليوم: {total}\nاستمروا، الغابة تكبر! 🍃",
    )


async def job_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    await context.bot.send_poll(
        chat_id=GROUP_CHAT_ID,
        question="⏱️ كم ساعة ذاكرت اليوم؟",
        options=["لسا ما بدأت 😅", "أقل من ساعة", "1-2 ساعة", "3-4 ساعات", "5+ ساعات 🔥"],
        is_anonymous=False,
    )


async def job_weekly_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = local_today()
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)

    rows = leaderboard(week_start)
    if rows:
        winner = rows[0]
        record_hall_of_fame(
            week_start, winner["user_id"], winner["name"], winner["batch"], winner["total"]
        )

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 نتيجة هذا الأسبوع (كل الدفعات):\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        batch_tag = f" [{r['batch']}]" if r["batch"] else ""
        lines.append(f"{prefix} {r['name']}{batch_tag} — {r['total']} دقيقة")

    best_user, delta = most_improved(week_start, last_week_start)
    if best_user and delta > 0:
        lines.append(f"\n📈 الأكثر تحسنًا: {best_user} (+{delta} دقيقة عن الأسبوع الماضي)")

    # Per-batch breakdown, only for batches with any activity this week
    active_batches = batches_with_activity(week_start)
    for batch in active_batches:
        batch_rows = leaderboard(week_start, limit=3, batch=batch)
        if not batch_rows:
            continue
        lines.append(f"\n🎓 أفضل 3 في {batch}:")
        for i, r in enumerate(batch_rows):
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{prefix} {r['name']} — {r['total']} دقيقة")

    # Batch-vs-batch: both the raw total (recruitment incentive) and the
    # per-capita average (fairness), for every batch with at least one
    # registered member.
    registered_batches = [b for b in VALID_BATCHES if batch_member_count(b) > 0]
    if len(registered_batches) > 1:
        totals = batch_totals(week_start)
        averages = batch_averages(week_start)
        ranked = sorted(registered_batches, key=lambda b: totals.get(b, 0), reverse=True)
        lines.append("\n⚔️ حرب الدفعات (إجمالي / متوسط الفرد):")
        for b in ranked:
            lines.append(f"— {b}: {totals.get(b, 0)} د إجمالي، {averages.get(b, 0)} د/فرد")
        last_batch = ranked[-1]
        lines.append(f"\n⚠️ {last_batch} في آخر الترتيب هذا الأسبوع — ادعوا أصحابكم قبل لا تخسرون!")

    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="\n".join(lines))


async def job_monthly_recap(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = local_today()
    month_start = today.replace(day=1)
    conn = db()
    total = conn.execute(
        "SELECT COALESCE(SUM(minutes),0) AS m FROM sessions WHERE session_date >= ?",
        (month_start.isoformat(),),
    ).fetchone()["m"]
    best_day = conn.execute(
        "SELECT session_date, SUM(minutes) AS m FROM sessions WHERE session_date >= ? "
        "GROUP BY session_date ORDER BY m DESC LIMIT 1",
        (month_start.isoformat(),),
    ).fetchone()
    conn.close()

    text = f"📅 ملخص الشهر:\n— إجمالي الدقائق: {total} ({total // 60} ساعة)\n"
    if best_day:
        text += f"— أكثر يوم نشاطًا: {best_day['session_date']} ({best_day['m']} دقيقة)\n"
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable first.")

    init_db()

    async def post_init(app: Application):
        await start_ws_server(app)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("setname", cmd_setname))
    app.add_handler(CommandHandler("setschedule", cmd_setschedule))
    app.add_handler(CommandHandler("exammode", cmd_exammode))
    app.add_handler(CommandHandler("findpartner", cmd_findpartner))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Schedule automated posts (times are local per TZ_OFFSET_HOURS, expressed as UTC here)
    jq = app.job_queue
    # Daily summary at 23:00 local
    jq.run_daily(job_daily_summary, time=dtime(hour=(23 - TZ_OFFSET_HOURS) % 24))
    # Daily "which tree" poll at 17:00 local
    jq.run_daily(job_daily_poll, time=dtime(hour=(17 - TZ_OFFSET_HOURS) % 24))
    # Weekly leaderboard every Saturday at 23:30 local (weekday 5 = Saturday)
    jq.run_daily(
        job_weekly_leaderboard,
        time=dtime(hour=(23 - TZ_OFFSET_HOURS) % 24, minute=30),
        days=(5,),
    )
    # Monthly recap on the 1st at 09:00 local
    jq.run_monthly(job_monthly_recap, when=dtime(hour=(9 - TZ_OFFSET_HOURS) % 24), day=1)
    # Safety-net website sync every 30 minutes, in case a per-log push fails
    jq.run_repeating(job_sync_website, interval=1800, first=60)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
