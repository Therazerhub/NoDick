"""NoDick database — SQLite (local) / PostgreSQL (Render) dual backend.

Auto-detection: if DATABASE_URL env var is set → PostgreSQL, else → SQLite.
All functions work identically regardless of backend.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import settings

# ── Backend detection ──────────────────────────────────────────────────────

_using_pg = bool(settings.database_url)

# ── PostgreSQL helpers ─────────────────────────────────────────────────────

if _using_pg:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    _pg_conn = None

    def _get_pg():
        global _pg_conn
        if _pg_conn is None or _pg_conn.closed:
            _pg_conn = psycopg2.connect(settings.database_url)
            _pg_conn.autocommit = True
        return _pg_conn

    @contextmanager
    def connect():
        """Context manager yielding the global PG connection (compat with SQLite API)."""
        yield _get_pg()

    def _fetchall(sql, params=None):
        conn = _get_pg()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall() or []

    def _fetchone(sql, params=None):
        conn = _get_pg()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _execute(sql, params=None):
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur

    def _lastrowid(cur) -> int:
        return cur.fetchone()[0] if cur.description else 0

# ── SQLite helpers ─────────────────────────────────────────────────────────

else:
    import sqlite3

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

    def _fetchall(sql, params=None):
        with connect() as conn:
            return conn.execute(sql, params or ()).fetchall() or []

    def _fetchone(sql, params=None):
        with connect() as conn:
            return conn.execute(sql, params or ()).fetchone()

    def _execute(sql, params=None):
        with connect() as conn:
            cur = conn.execute(sql, params or ())
            return cur

    def _lastrowid(cur) -> int:
        return cur.lastrowid or 0

# ── Schema ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize tables and run lightweight migrations."""
    if _using_pg:
        _init_pg()
    else:
        _init_sqlite()


def _init_pg():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                file_id TEXT UNIQUE NOT NULL,
                title TEXT,
                tags TEXT,
                category TEXT,
                duration INTEGER DEFAULT 0,
                added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                view_count INTEGER DEFAULT 0,
                source_channel TEXT,
                source_chat_id BIGINT,
                source_message_id BIGINT,
                file_unique_id TEXT,
                file_size BIGINT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS video_metadata (
                video_id INTEGER PRIMARY KEY REFERENCES videos(id),
                stashdb_scene_id TEXT,
                stashdb_performer TEXT,
                stashdb_title TEXT,
                stashdb_studio TEXT,
                stashdb_confidence REAL,
                corrected_performer TEXT,
                corrected_title TEXT,
                corrected_studio TEXT,
                corrected_by_user BIGINT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                video_id INTEGER REFERENCES videos(id),
                UNIQUE(user_id, video_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                show_action_buttons INTEGER DEFAULT 1,
                max_size_mb INTEGER,
                is_admin INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS import_jobs (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                total_checked INTEGER DEFAULT 0,
                videos_found INTEGER DEFAULT 0,
                saved INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                error TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                created_by BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Default settings
        cur.execute(
            "INSERT INTO bot_settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            ("action_buttons_enabled", "1"),
        )
        # Column migrations (check existence via information_schema)
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='videos'"
        )
        existing = {
            row[0]
            for row in cur.fetchall()
        }
        pg_migrations = {
            "source_chat_id": "ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_chat_id BIGINT",
            "source_message_id": "ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_message_id BIGINT",
            "file_unique_id": "ALTER TABLE videos ADD COLUMN IF NOT EXISTS file_unique_id TEXT",
            "file_size": "ALTER TABLE videos ADD COLUMN IF NOT EXISTS file_size BIGINT",
            "source_channel": "ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_channel TEXT",
        }
        for col, sql in pg_migrations.items():
            if col not in existing:
                try:
                    cur.execute(sql)
                except Exception:
                    pass

        # user_settings migrations
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='user_settings'"
        )
        us_existing = {row[0] for row in cur.fetchall()}
        if "max_size_mb" not in us_existing:
            try:
                cur.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS max_size_mb INTEGER")
            except Exception:
                pass


def _init_sqlite():
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
        max_size_mb INTEGER,
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
    import sqlite3

    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
            ("action_buttons_enabled", "1"),
        )
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
                    pass

        us_existing = {row[1] for row in conn.execute("PRAGMA table_info(user_settings)")}
        if "max_size_mb" not in us_existing:
            try:
                conn.execute("ALTER TABLE user_settings ADD COLUMN max_size_mb INTEGER")
            except sqlite3.OperationalError:
                pass


# ── Video CRUD ──────────────────────────────────────────────────────────

def _size_filter(max_size_mb: Optional[int], col: str = "file_size") -> tuple[str, tuple]:
    """Build (sql_chunk, params) for the size filter.

    Videos with unknown size pass through (NULL), so the filter is
    best-effort until backfill fills in the sizes.
    """
    if not max_size_mb:
        return "", ()
    ph = "%s" if _using_pg else "?"
    return f" AND ({col} IS NULL OR {col} <= {ph})", (max_size_mb * 1024 * 1024,)


def update_video_size(video_id: int, file_size: int) -> None:
    """Fill in a missing file_size (used by lazy + bulk backfill)."""
    ph = "%s" if _using_pg else "?"
    _execute(
        f"UPDATE videos SET file_size = {ph} WHERE id = {ph} AND (file_size IS NULL OR file_size <= 0)",
        (file_size, video_id),
    )


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
    if _using_pg:
        cur = _execute(
            """INSERT INTO videos (file_id, title, duration, category, tags,
                source_channel, source_chat_id, source_message_id,
                file_unique_id, file_size)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (file_id) DO NOTHING""",
            (file_id, title, duration or 0, category, tags,
             source_channel, source_chat_id, source_message_id,
             file_unique_id, file_size),
        )
        return cur.rowcount > 0
    else:
        cur = _execute(
            """INSERT OR IGNORE INTO videos
               (file_id, title, duration, category, tags,
                source_channel, source_chat_id, source_message_id,
                file_unique_id, file_size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_id, title, duration or 0, category, tags,
             source_channel, source_chat_id, source_message_id,
             file_unique_id, file_size),
        )
        return cur.rowcount > 0


def get_video(video_id: int) -> Optional[dict]:
    return _fetchone("SELECT * FROM videos WHERE id = %s" if _using_pg else "SELECT * FROM videos WHERE id = ?", (video_id,))


def random_video(max_size_mb: Optional[int] = None) -> Optional[dict]:
    if max_size_mb:
        ph = "%s" if _using_pg else "?"
        return _fetchone(
            f"SELECT * FROM videos WHERE file_size IS NULL OR file_size <= {ph} ORDER BY RANDOM() LIMIT 1",
            (max_size_mb * 1024 * 1024,),
        )
    return _fetchone("SELECT * FROM videos ORDER BY RANDOM() LIMIT 1")


def increment_view(video_id: int) -> None:
    _execute(
        "UPDATE videos SET view_count = view_count + 1 WHERE id = %s" if _using_pg else "UPDATE videos SET view_count = view_count + 1 WHERE id = ?",
        (video_id,),
    )


def search_videos(query: str, page: int = 0, per_page: int = 10, max_size_mb: Optional[int] = None) -> tuple[list, int]:
    """Search videos by title/category/tags. Returns (rows, total_count)."""
    term = f"%{query}%"
    like_op = "ILIKE" if _using_pg else "LIKE"
    size_chunk, size_params = _size_filter(max_size_mb)
    if _using_pg:
        total_row = _fetchone(
            f"SELECT COUNT(*) AS cnt FROM videos WHERE (title {like_op} %s OR category {like_op} %s OR tags {like_op} %s){size_chunk}",
            (term, term, term) + size_params,
        )
        total = total_row["cnt"]
        rows = _fetchall(
            f"SELECT id, title, duration FROM videos WHERE (title {like_op} %s OR category {like_op} %s OR tags {like_op} %s){size_chunk} ORDER BY id DESC LIMIT %s OFFSET %s",
            (term, term, term) + size_params + (per_page, page * per_page),
        )
    else:
        total = _fetchone(
            f"SELECT COUNT(*) FROM videos WHERE (title LIKE ? OR category LIKE ? OR tags LIKE ?){size_chunk}",
            (term, term, term) + size_params,
        )[0]
        rows = _fetchall(
            f"SELECT id, title, duration FROM videos WHERE (title LIKE ? OR category LIKE ? OR tags LIKE ?){size_chunk} ORDER BY id DESC LIMIT ? OFFSET ?",
            (term, term, term) + size_params + (per_page, page * per_page),
        )
    return rows, total



def video_count() -> int:
    row = _fetchone("SELECT COUNT(*) AS cnt FROM videos")
    return row["cnt"] if _using_pg else row[0]

def total_views() -> int:
    row = _fetchone("SELECT COALESCE(SUM(view_count), 0) AS cnt FROM videos")
    return row["cnt"] if _using_pg else row[0]

def category_count() -> int:
    row = _fetchone("SELECT COUNT(DISTINCT category) AS cnt FROM videos WHERE category IS NOT NULL")
    return row["cnt"] if _using_pg else row[0] if row else 0


def get_categories() -> list[dict]:
    return _fetchall(
        "SELECT category, COUNT(*) as count FROM videos WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"
    )


def get_videos_by_category(category: str, page: int = 0, per_page: int = 10, max_size_mb: Optional[int] = None) -> tuple[list, int]:
    size_chunk, size_params = _size_filter(max_size_mb)
    if _using_pg:
        total_row = _fetchone(
            f"SELECT COUNT(*) AS cnt FROM videos WHERE category = %s{size_chunk}", (category,) + size_params,
        )
        total = total_row["cnt"]
        rows = _fetchall(
            f"SELECT id, title, duration FROM videos WHERE category = %s{size_chunk} ORDER BY id DESC LIMIT %s OFFSET %s",
            (category,) + size_params + (per_page, page * per_page),
        )
    else:
        total = _fetchone(
            f"SELECT COUNT(*) FROM videos WHERE category = ?{size_chunk}", (category,) + size_params,
        )[0]
        rows = _fetchall(
            f"SELECT id, title, duration FROM videos WHERE category = ?{size_chunk} ORDER BY id DESC LIMIT ? OFFSET ?",
            (category,) + size_params + (per_page, page * per_page),
        )
    return rows, total


# ── Metadata ─────────────────────────────────────────────────────────────

def upsert_video_metadata(video_id: int, **fields) -> None:
    """Upsert stash/corrected metadata for a video."""
    if not fields:
        return
    import datetime

    fields["last_updated"] = datetime.datetime.utcnow().isoformat()
    cols = ", ".join(fields)
    vals = ", ".join(f"%" if _using_pg else "?" for _ in fields)
    placeholders = [f"{k} = %s" if _using_pg else f"{k} = ?" for k in fields]
    updates = ", ".join(placeholders)

    if _using_pg:
        _execute(
            f"""INSERT INTO video_metadata (video_id, {cols})
               VALUES (%s, {vals})
               ON CONFLICT (video_id) DO UPDATE SET {updates}""",
            (video_id, *fields.values(), *fields.values()),
        )
    else:
        _execute(
            f"""INSERT INTO video_metadata (video_id, {cols})
               VALUES (?, {vals})
               ON CONFLICT(video_id) DO UPDATE SET {updates}""",
            (video_id, *fields.values(), *fields.values()),
        )


def get_video_metadata(video_id: int) -> Optional[dict]:
    return _fetchone(
        "SELECT * FROM video_metadata WHERE video_id = %s" if _using_pg else "SELECT * FROM video_metadata WHERE video_id = ?",
        (video_id,),
    )


# ── Favorites ─────────────────────────────────────────────────────────────

def toggle_favorite(user_id: int, video_id: int) -> bool:
    """Toggle favorite, return True if now favorited."""
    if _using_pg:
        cur = _execute(
            "INSERT INTO favorites (user_id, video_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, video_id),
        )
        if cur.rowcount:
            return True
        _execute(
            "DELETE FROM favorites WHERE user_id = %s AND video_id = %s",
            (user_id, video_id),
        )
        return False
    else:
        cur = _execute(
            "INSERT OR IGNORE INTO favorites (user_id, video_id) VALUES (?, ?)",
            (user_id, video_id),
        )
        if cur.rowcount:
            return True
        _execute(
            "DELETE FROM favorites WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        )
        return False


def get_favorites(user_id: int, page: int = 0, per_page: int = 10, max_size_mb: Optional[int] = None) -> tuple[list, int]:
    size_chunk, size_params = _size_filter(max_size_mb, "v.file_size")
    if _using_pg:
        total_row = _fetchone(
            f"SELECT COUNT(*) AS cnt FROM favorites f JOIN videos v ON v.id = f.video_id WHERE f.user_id = %s{size_chunk}",
            (user_id,) + size_params,
        )
        total = total_row["cnt"]
        rows = _fetchall(
            f"""SELECT v.id, v.title, v.duration FROM favorites f
               JOIN videos v ON v.id = f.video_id
               WHERE f.user_id = %s{size_chunk}
               ORDER BY f.id DESC LIMIT %s OFFSET %s""",
            (user_id,) + size_params + (per_page, page * per_page),
        )
    else:
        total = _fetchone(
            f"SELECT COUNT(*) FROM favorites f JOIN videos v ON v.id = f.video_id WHERE f.user_id = ?{size_chunk}",
            (user_id,) + size_params,
        )[0]
        rows = _fetchall(
            f"""SELECT v.id, v.title, v.duration FROM favorites f
               JOIN videos v ON v.id = f.video_id
               WHERE f.user_id = ?{size_chunk}
               ORDER BY f.id DESC LIMIT ? OFFSET ?""",
            (user_id,) + size_params + (per_page, page * per_page),
        )
    return rows, total


# ── Settings ──────────────────────────────────────────────────────────────

def get_bot_setting(key: str, default: str = "") -> str:
    row = _fetchone(
        "SELECT value FROM bot_settings WHERE key = %s" if _using_pg else "SELECT value FROM bot_settings WHERE key = ?",
        (key,),
    )
    return row["value"] if _using_pg and row else row[0] if row else default


def set_bot_setting(key: str, value: str) -> None:
    if _using_pg:
        _execute(
            """INSERT INTO bot_settings (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )
    else:
        _execute(
            """INSERT INTO bot_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )


def ensure_user_exists(user_id: int) -> None:
    is_admin = 1 if user_id == settings.admin_id else 0
    if _using_pg:
        _execute(
            """INSERT INTO user_settings (user_id, is_admin, last_seen) VALUES (%s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (user_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP""",
            (user_id, is_admin),
        )
    else:
        _execute(
            """INSERT INTO user_settings (user_id, is_admin) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP""",
            (user_id, is_admin),
        )


def user_setting(user_id: int, key: str) -> Optional[bool]:
    if key == "show_action_buttons":
        row = _fetchone(
            "SELECT show_action_buttons FROM user_settings WHERE user_id = %s" if _using_pg else "SELECT show_action_buttons FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        return bool(row["show_action_buttons"]) if _using_pg and row else bool(row[0]) if row else None
    return None


def set_user_setting(user_id: int, key: str, value) -> None:
    if key == "show_action_buttons":
        _execute(
            "UPDATE user_settings SET show_action_buttons = %s WHERE user_id = %s" if _using_pg else "UPDATE user_settings SET show_action_buttons = ? WHERE user_id = ?",
            (1 if value else 0, user_id),
        )


def get_user_size_limit(user_id: int) -> Optional[int]:
    """User's max video size in MB (None = unlimited)."""
    row = _fetchone(
        "SELECT max_size_mb FROM user_settings WHERE user_id = %s" if _using_pg else "SELECT max_size_mb FROM user_settings WHERE user_id = ?",
        (user_id,),
    )
    if not row:
        return None
    return row["max_size_mb"] if _using_pg else row[0]


def set_user_size_limit(user_id: int, mb: Optional[int]) -> None:
    """Set user's max video size in MB (None = unlimited)."""
    if _using_pg:
        _execute(
            """INSERT INTO user_settings (user_id, max_size_mb, last_seen)
               VALUES (%s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (user_id) DO UPDATE
               SET max_size_mb = EXCLUDED.max_size_mb, last_seen = CURRENT_TIMESTAMP""",
            (user_id, mb),
        )
    else:
        _execute(
            """INSERT INTO user_settings (user_id, max_size_mb, last_seen)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE
               SET max_size_mb = excluded.max_size_mb, last_seen = CURRENT_TIMESTAMP""",
            (user_id, mb),
        )


# ── Import Jobs ────────────────────────────────────────────────────────────

def create_import_job(channel_id: str, created_by: int) -> int:
    if _using_pg:
        cur = _execute(
            "INSERT INTO import_jobs (channel_id, created_by) VALUES (%s, %s) RETURNING id",
            (str(channel_id), created_by),
        )
        return _lastrowid(cur)
    else:
        cur = _execute(
            "INSERT INTO import_jobs (channel_id, created_by) VALUES (?, ?)",
            (str(channel_id), created_by),
        )
        return _lastrowid(cur)


def update_import_job(job_id: int, **fields) -> None:
    if not fields:
        return
    if _using_pg:
        sets = ", ".join(f"{k} = %s" for k in fields)
        _execute(
            f"UPDATE import_jobs SET {sets} WHERE id = %s",
            (*fields.values(), job_id),
        )
    else:
        sets = ", ".join(f"{k} = ?" for k in fields)
        _execute(
            f"UPDATE import_jobs SET {sets} WHERE id = ?",
            (*fields.values(), job_id),
        )


def latest_import_job() -> Optional[dict]:
    return _fetchone("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1")


# ── Multi-part video helpers ─────────────────────────────────────────────

def find_sibling_parts(video_id: int, title: str) -> list[dict]:
    """Find multi-part siblings for a video."""
    from .utils import parse_part_info

    base, _ = parse_part_info(title)
    if not base:
        return []

    rows = _fetchall(
        "SELECT id, title FROM videos WHERE id != %s AND title LIKE %s ORDER BY title ASC" if _using_pg
        else "SELECT id, title FROM videos WHERE id != ? AND title LIKE ? ORDER BY title ASC",
        (video_id, f"{base} part%"),
    )

    if not rows:
        return []

    siblings = []
    for row in rows:
        _, part = parse_part_info(row["title"])
        if part:
            siblings.append({"id": row["id"], "part": part})

    if not siblings:
        return []

    _, current_part = parse_part_info(title)
    siblings.append({"id": video_id, "part": current_part or 0})
    siblings.sort(key=lambda x: (x["part"], x["id"]))
    total = len(siblings)
    return [
        {"id": s["id"], "part": i + 1, "total": total}
        for i, s in enumerate(siblings)
    ]
