"""NoDick database — merged schema from both bots"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import settings
from .utils import parse_part_info

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT UNIQUE NOT NULL,
    title TEXT,
    tags TEXT,
    category TEXT,
    duration INTEGER DEFAULT 0,
    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    view_count INTEGER DEFAULT 0,
    source_channel TEXT,
    source_chat_id INTEGER,
    source_message_id INTEGER,
    file_unique_id TEXT,
    file_size INTEGER
);

CREATE TABLE IF NOT EXISTS video_metadata (
    video_id INTEGER PRIMARY KEY,
    stashdb_scene_id TEXT,
    stashdb_performer TEXT,
    stashdb_title TEXT,
    stashdb_studio TEXT,
    stashdb_confidence REAL,
    corrected_performer TEXT,
    corrected_title TEXT,
    corrected_studio TEXT,
    corrected_by_user INTEGER,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id)
);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    video_id INTEGER,
    FOREIGN KEY (video_id) REFERENCES videos(id),
    UNIQUE(user_id, video_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    show_action_buttons INTEGER DEFAULT 1,
    is_admin INTEGER DEFAULT 0,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    total_checked INTEGER DEFAULT 0,
    videos_found INTEGER DEFAULT 0,
    saved INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    error TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def db_path() -> Path:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = settings.root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Initialize all tables and run lightweight migrations."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
            ("action_buttons_enabled", "1"),
        )
        # Column migrations for videos table
        existing = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
        migrations = {
            "source_chat_id": "ALTER TABLE videos ADD COLUMN source_chat_id INTEGER",
            "source_message_id": "ALTER TABLE videos ADD COLUMN source_message_id INTEGER",
            "file_unique_id": "ALTER TABLE videos ADD COLUMN file_unique_id TEXT",
            "file_size": "ALTER TABLE videos ADD COLUMN file_size INTEGER",
            "source_channel": "ALTER TABLE videos ADD COLUMN source_channel TEXT",
        }
        for col, sql in migrations.items():
            if col not in existing:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists


# ── Video CRUD ──────────────────────────────────────────────────────────


def upsert_video(
    *,
    file_id: str,
    title: str,
    duration: int = 0,
    category: Optional[str] = None,
    source_channel: Optional[str] = None,
    source_chat_id: Optional[int] = None,
    source_message_id: Optional[int] = None,
    file_unique_id: Optional[str] = None,
    file_size: Optional[int] = None,
    tags: Optional[str] = None,
) -> bool:
    """Insert a video, return True if new, False if already existed."""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO videos
            (file_id, title, duration, category, tags,
             source_channel, source_chat_id, source_message_id,
             file_unique_id, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                title,
                duration or 0,
                category,
                tags,
                source_channel,
                source_chat_id,
                source_message_id,
                file_unique_id,
                file_size,
            ),
        )
        return cur.rowcount > 0


def get_video(video_id: int) -> Optional[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,)
        ).fetchone()


def random_video() -> Optional[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM videos ORDER BY RANDOM() LIMIT 1"
        ).fetchone()


def increment_view(video_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET view_count = view_count + 1 WHERE id = ?",
            (video_id,),
        )


def search_videos(query: str, page: int = 0, per_page: int = 10) -> tuple[list, int]:
    """Search videos by title/category/tags. Returns (rows, total_count)."""
    term = f"%{query}%"
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE title LIKE ? OR category LIKE ? OR tags LIKE ?",
            (term, term, term),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, duration FROM videos WHERE title LIKE ? OR category LIKE ? OR tags LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (term, term, term, per_page, page * per_page),
        ).fetchall()
    return rows, total


def video_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def total_views() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(view_count), 0) FROM videos"
        ).fetchone()[0]


def category_count() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(DISTINCT category) FROM videos WHERE category IS NOT NULL"
        ).fetchone()[0]


def get_categories() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT category, COUNT(*) as count FROM videos WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"
        ).fetchall()


def get_videos_by_category(category: str, page: int = 0, per_page: int = 10) -> tuple[list, int]:
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE category = ?", (category,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, duration FROM videos WHERE category = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (category, per_page, page * per_page),
        ).fetchall()
    return rows, total


# ── Metadata ─────────────────────────────────────────────────────────────


def upsert_video_metadata(video_id: int, **fields) -> None:
    """Upsert stash/ corrected metadata for a video."""
    fields["last_updated"] = "CURRENT_TIMESTAMP"
    keys = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [video_id]
    with connect() as conn:
        conn.execute(
            f"""
            INSERT INTO video_metadata (video_id, {', '.join(fields)})
            VALUES (?, {', '.join('?' for _ in fields)})
            ON CONFLICT(video_id) DO UPDATE SET {keys}
            """,
            (video_id, *values[1:], video_id),
        )


def get_video_metadata(video_id: int) -> Optional[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM video_metadata WHERE video_id = ?", (video_id,)
        ).fetchone()


# ── Favorites ─────────────────────────────────────────────────────────────


def toggle_favorite(user_id: int, video_id: int) -> bool:
    """Toggle favorite, return True if now favorited."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO favorites (user_id, video_id) VALUES (?, ?)",
            (user_id, video_id),
        )
        if cur.rowcount:
            return True
        conn.execute(
            "DELETE FROM favorites WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        )
        return False


def get_favorites(
    user_id: int, page: int = 0, per_page: int = 10
) -> tuple[list, int]:
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT v.id, v.title, v.duration FROM favorites f
            JOIN videos v ON v.id = f.video_id
            WHERE f.user_id = ?
            ORDER BY f.id DESC LIMIT ? OFFSET ?
            """,
            (user_id, per_page, page * per_page),
        ).fetchall()
    return rows, total


# ── Settings ──────────────────────────────────────────────────────────────


def get_bot_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default


def set_bot_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO bot_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (key, value),
        )


def ensure_user_exists(user_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_settings (user_id, is_admin) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""",
            (user_id, 1 if user_id == settings.admin_id else 0),
        )


def user_setting(user_id: int, key: str) -> Optional[bool]:
    if key == "show_action_buttons":
        with connect() as conn:
            row = conn.execute(
                "SELECT show_action_buttons FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return bool(row[0]) if row else None
    return None


def set_user_setting(user_id: int, key: str, value) -> None:
    if key == "show_action_buttons":
        with connect() as conn:
            conn.execute(
                "UPDATE user_settings SET show_action_buttons = ? WHERE user_id = ?",
                (1 if value else 0, user_id),
            )


# ── Import Jobs ────────────────────────────────────────────────────────────


def create_import_job(channel_id: str, created_by: int) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO import_jobs (channel_id, created_by) VALUES (?, ?)",
            (str(channel_id), created_by),
        )
        return int(cur.lastrowid)


def update_import_job(job_id: int, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with connect() as conn:
        conn.execute(f"UPDATE import_jobs SET {keys} WHERE id = ?", values)


def latest_import_job() -> Optional[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()


# ── Multi-part video helpers ─────────────────────────────────────────────


def find_sibling_parts(video_id: int, title: str) -> list[dict]:
    """Find multi-part siblings for a video.

    Parses the title for a 'partXXX' suffix, then finds other videos with the
    same base name. Returns sorted list of {id, part, total} or empty list.

    Example: title 'SiaSiberia HotAsHerFuck 1080p part002' finds all
    'SiaSiberia HotAsHerFuck 1080p partXXX' videos and returns them ordered.
    """
    base, _ = parse_part_info(title)
    if not base:
        return []

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title FROM videos WHERE id != ? AND title LIKE ? ORDER BY title ASC",
            (video_id, f"{base} part%"),
        ).fetchall()

    if not rows:
        return []

    # Build list with part numbers extracted from each sibling
    siblings = []
    for row in rows:
        _, part = parse_part_info(row["title"])
        if part:
            siblings.append({"id": row["id"], "part": part})

    if not siblings:
        return []

    # Add current video
    _, current_part = parse_part_info(title)
    siblings.append({"id": video_id, "part": current_part or 0})

    # Sort by part number, then reassign sequential indices
    siblings.sort(key=lambda x: x["part"])
    total = len(siblings)
    return [
        {"id": s["id"], "part": i + 1, "total": total}
        for i, s in enumerate(siblings)
    ]
