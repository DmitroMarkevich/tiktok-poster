"""Comment de-duplication — synchronous SQLite helper.

Why sync sqlite3 (not the project's async SQLAlchemy): the commenter runs inside a
SEPARATE event loop spun up in a worker thread (see bot/handlers/comments.py
_browser_work_sync). The async engine is bound to the main loop, so touching it from
the worker loop raises "Future attached to a different loop". These dedup ops are
tiny SELECT/INSERT/UPDATE, so a short-lived blocking sqlite3 connection is the safe,
simple choice — SQLite serialises writes and we set a busy timeout for the rare
contention with the main-loop async engine.

Two layers, ported from multicombine:
  • commented_videos — per-account history: never comment the same video twice.
  • video_claims     — global registry: one account per video per run (a failed post
                       releases the claim so another account may try).
"""
import re
import sqlite3
from datetime import datetime, timedelta

from config import DATABASE_URL

_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def _db_path() -> str:
    # DATABASE_URL looks like "sqlite+aiosqlite:///./tiktok_bot.db"
    return DATABASE_URL.split(":///", 1)[-1] if ":///" in DATABASE_URL else "tiktok_bot.db"


def extract_video_id(url: str) -> str:
    """Numeric id from a TikTok video URL; falls back to the full URL as a stable key."""
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else (url or "").strip()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


_SCHEMA_READY = False


def _ensure_schema() -> None:
    """Add the shadowban-tracking columns to commented_videos if missing. Idempotent:
    SQLite has no 'ADD COLUMN IF NOT EXISTS', so each ALTER is tried and the
    'duplicate column' error is swallowed. Runs once per process."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn = _connect()
    try:
        for ddl in (
            "ALTER TABLE commented_videos ADD COLUMN comment_text TEXT",
            "ALTER TABLE commented_videos ADD COLUMN visible INTEGER",      # NULL=unchecked 1=visible 0=shadowbanned
            "ALTER TABLE commented_videos ADD COLUMN verified_at TIMESTAMP",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
        _SCHEMA_READY = True
    finally:
        conn.close()


def already_commented(account_id: int, video_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM commented_videos WHERE account_id=? AND video_id=? LIMIT 1",
            (account_id, video_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_comment(account_id: int, video_id: str, status: str = "success",
                   comment_text: str = "") -> None:
    _ensure_schema()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO commented_videos "
            "(account_id, video_id, status, comment_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, video_id, status, comment_text, datetime.utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def set_visibility(account_id: int, video_id: str, visible) -> None:
    """Record the shadowban verdict for a comment. `visible`: True / False / None
    (None = inconclusive, stored as NULL so trust score ignores it)."""
    _ensure_schema()
    val = None if visible is None else (1 if visible else 0)
    conn = _connect()
    try:
        conn.execute(
            "UPDATE commented_videos SET visible=?, verified_at=? "
            "WHERE account_id=? AND video_id=?",
            (val, datetime.utcnow(), account_id, video_id),
        )
        conn.commit()
    finally:
        conn.close()


def comments_in_last(account_id: int, hours: float = 24) -> int:
    """Count SUCCESSFUL comments this account made within the last `hours` (rolling window).
    Used for circadian daily caps — a rolling window avoids timezone day-boundary edge cases
    and counts only `status='success'` so failed/blocked attempts don't eat the quota."""
    _ensure_schema()
    conn = _connect()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        row = conn.execute(
            "SELECT COUNT(*) FROM commented_videos "
            "WHERE account_id=? AND status='success' AND created_at>=?",
            (account_id, cutoff),
        ).fetchone()
        return row[0] or 0
    finally:
        conn.close()


def account_trust(account_id: int) -> tuple:
    """Returns (survived, shadowbanned, checked, pct) over all VERIFIED comments for the
    account. `checked` excludes inconclusive (NULL) rows; pct is survived/checked*100
    (0.0 when nothing checked yet)."""
    _ensure_schema()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN visible=1 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN visible=0 THEN 1 ELSE 0 END) "
            "FROM commented_videos WHERE account_id=? AND visible IS NOT NULL",
            (account_id,),
        ).fetchone()
        survived = row[0] or 0
        banned = row[1] or 0
        checked = survived + banned
        pct = (survived / checked * 100.0) if checked else 0.0
        return survived, banned, checked, pct
    finally:
        conn.close()


# A 'pending' claim older than this is treated as orphaned (its run was killed before
# it could mark success/failed) and becomes re-takeable. Without this, a crash/restart
# mid-run leaves the video blocked forever — observed live after killing runs to deploy
# fixes, which silently emptied later runs ("all videos зайнято").
_CLAIM_TTL = timedelta(minutes=30)


def try_claim(video_id: str, account_id: int, topic: str = "") -> bool:
    """Atomically claim a video for this account. Returns True if claimed, False if
    another account already holds an active claim. Re-takeable when the previous claim
    FAILED, or is a 'pending' older than _CLAIM_TTL (orphaned by a dead run)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO video_claims (video_id, account_id, topic, status, claimed_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (video_id, account_id, topic, datetime.utcnow()),
        )
        conn.commit()
        if cur.rowcount > 0:
            return True  # fresh claim inserted
        # Row already exists — re-claim if the previous holder failed OR left a stale
        # 'pending' (run died before resolving it).
        row = conn.execute(
            "SELECT status, claimed_at FROM video_claims WHERE video_id=?", (video_id,)
        ).fetchone()
        retakeable = False
        if row:
            status, claimed_at = row[0], row[1]
            if status == "failed":
                retakeable = True
            elif status == "pending" and claimed_at:
                try:
                    ts = claimed_at if isinstance(claimed_at, datetime) else \
                        datetime.fromisoformat(str(claimed_at))
                    if datetime.utcnow() - ts > _CLAIM_TTL:
                        retakeable = True
                except (TypeError, ValueError):
                    pass
        if retakeable:
            conn.execute(
                "UPDATE video_claims SET account_id=?, topic=?, status='pending', claimed_at=? "
                "WHERE video_id=?",
                (account_id, topic, datetime.utcnow(), video_id),
            )
            conn.commit()
            return conn.total_changes > 0
        return False
    finally:
        conn.close()


def mark_success(video_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE video_claims SET status='success' WHERE video_id=?", (video_id,))
        conn.commit()
    finally:
        conn.close()


def mark_failed(video_id: str) -> None:
    """Release the claim so another account may try this video."""
    conn = _connect()
    try:
        conn.execute("UPDATE video_claims SET status='failed' WHERE video_id=?", (video_id,))
        conn.commit()
    finally:
        conn.close()
