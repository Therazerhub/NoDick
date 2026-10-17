from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from nodick.telegram import app


ROOT = Path(__file__).resolve().parents[1]


def _run_db_script(tmp_path: Path, body: str) -> dict:
    db_path = tmp_path / "test.db"
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["DB_PATH"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_quota_reservation_is_atomic_under_concurrency(tmp_path):
    result = _run_db_script(
        tmp_path,
        """
import json
from concurrent.futures import ThreadPoolExecutor
from nodick import db

db.init_db()
db.ensure_user_exists(101)
db._execute("UPDATE user_settings SET quota_used=0, quota_limit=1, quota_date=date('now') WHERE user_id=?", (101,))
with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(lambda _: db.increment_quota(101), range(2)))
quota = db.get_user_quota(101)
print(json.dumps({"wins": sum(outcomes), "used": quota["used"]}))
""",
    )
    assert result == {"wins": 1, "used": 1}


def test_daily_rollover_and_reservations_share_one_transaction(tmp_path):
    result = _run_db_script(
        tmp_path,
        """
import json
from concurrent.futures import ThreadPoolExecutor
from nodick import db

db.init_db()
db.ensure_user_exists(102)
db._execute("UPDATE user_settings SET quota_used=4, quota_limit=5, quota_date='2000-01-01' WHERE user_id=?", (102,))
with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(lambda _: db.increment_quota(102), range(2)))
quota = db.get_user_quota(102)
print(json.dumps({"wins": sum(outcomes), "used": quota["used"], "limit": quota["limit"]}))
""",
    )
    assert result == {"wins": 2, "used": 2, "limit": 6}


def test_referral_activates_on_first_delivery_and_milestone_once(tmp_path):
    result = _run_db_script(
        tmp_path,
        """
import json
from nodick import db

db.init_db()
db.ensure_user_exists(1)
db.grant_premium(1)
for referred in range(10, 19):
    db.ensure_user_exists(referred)
    db._execute("INSERT INTO referrals (referrer_id, referred_id, bonus_granted) VALUES (?, ?, ?)", (1, referred, 10))
db.ensure_user_exists(99)
assert db.set_pending_referral(1, 99)
first = db.activate_pending_referral(99, 10)
second = db.activate_pending_referral(99, 10)
row = db._fetchone("SELECT quota_limit, is_premium, premium_until, pending_referrer_id, referred_by FROM user_settings WHERE user_id=?", (1,))
referred = db._fetchone("SELECT pending_referrer_id, referred_by FROM user_settings WHERE user_id=?", (99,))
print(json.dumps({
    "count": first["count"],
    "premium": first["premium_awarded"],
    "second": second,
    "quota_limit": row["quota_limit"],
    "is_premium": row["is_premium"],
    "premium_until": row["premium_until"],
    "pending": referred["pending_referrer_id"],
    "referred_by": referred["referred_by"],
}))
""",
    )
    assert result == {
        "count": 10,
        "premium": True,
        "second": None,
        "quota_limit": 15,
        "is_premium": 1,
        "premium_until": None,
        "pending": None,
        "referred_by": 1,
    }


def test_pending_referrer_cannot_be_overwritten(tmp_path):
    result = _run_db_script(
        tmp_path,
        """
import json
from nodick import db

db.init_db()
for user_id in (1, 2, 99):
    db.ensure_user_exists(user_id)
first = db.set_pending_referral(1, 99)
second = db.set_pending_referral(2, 99)
row = db._fetchone("SELECT pending_referrer_id FROM user_settings WHERE user_id=?", (99,))
print(json.dumps({"first": first, "second": second, "pending": row["pending_referrer_id"]}))
""",
    )
    assert result == {"first": True, "second": False, "pending": 1}


def test_fresh_db_has_hot_path_indexes_and_pending_referral_column(tmp_path):
    result = _run_db_script(
        tmp_path,
        """
import json
from nodick import db

db.init_db()
with db.connect() as conn:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(user_settings)")]
    indexes = [row[1] for row in conn.execute("PRAGMA index_list(videos)")]
print(json.dumps({"pending": "pending_referrer_id" in columns, "category_index": "idx_videos_category_id" in indexes}))
""",
    )
    assert result == {"pending": True, "category_index": True}


def test_random_video_is_unbiased_across_sparse_ids_and_keeps_size_filter(tmp_path):
    result = _run_db_script(
        tmp_path,
        """
import json
import random
from nodick import db

db.init_db()
with db.connect() as conn:
    conn.executemany(
        'INSERT INTO videos(id, file_id, title, file_size, duration) VALUES (?, ?, ?, ?, ?)',
        [
            (1, 'small-a', 'small a', 10 * 1024 * 1024, 120),
            (5000, 'large', 'large', 900 * 1024 * 1024, 120),
            (7000, 'short', 'short clip', 10 * 1024 * 1024, 3),
            (8000, 'gif', 'reaction.gif', 10 * 1024 * 1024, 120),
            (10000, 'small-b', 'small b', 10 * 1024 * 1024, 120),
            (10001, 'small-c', 'small c', 10 * 1024 * 1024, 120),
        ],
    )
offsets = iter([0, 1, 2])
random.randrange = lambda count: next(offsets)
rows = [db.random_video(max_size_mb=100) for _ in range(3)]
print(json.dumps([row['file_id'] for row in rows]))
""",
    )
    assert result == ["small-a", "small-b", "small-c"]


def test_failed_delivery_refunds_reserved_quota(monkeypatch):
    refunds = []
    monkeypatch.setattr(app, "_is_admin", lambda _update: False)
    monkeypatch.setattr(app, "is_user_premium", lambda _uid: False)
    monkeypatch.setattr(app, "increment_quota", lambda _uid: True)
    monkeypatch.setattr(app, "refund_quota", refunds.append)

    async def missing(*_args, **_kwargs):
        return False

    monkeypatch.setattr(app, "_enrich_and_send", missing)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    delivered = asyncio.run(
        app._deliver_with_quota(update, object(), 42, allow_redirect=False)
    )
    assert delivered is False
    assert refunds == [7]


def test_regular_user_cannot_index_video(monkeypatch):
    replies = []
    writes = []
    monkeypatch.setattr(app, "_is_admin", lambda _update: False)
    monkeypatch.setattr(app, "upsert_video", lambda **kwargs: writes.append(kwargs))

    class Message:
        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(message=Message())
    asyncio.run(app.handle_video(update, SimpleNamespace()))
    assert writes == []
    assert replies and "Only approved admins" in replies[0]


def test_search_pagination_uses_short_callback_state(monkeypatch):
    sent = []
    monkeypatch.setattr(
        app,
        "search_videos",
        lambda *_args, **_kwargs: ([{"id": 1, "title": "Result", "duration": 1}], 30),
    )
    monkeypatch.setattr(app, "get_user_size_limit", lambda _uid: None)

    class Message:
        async def reply_text(self, text, **kwargs):
            sent.append((text, kwargs["reply_markup"]))

    query = "🔥" * 100
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=5),
        callback_query=None,
        message=Message(),
    )
    context = SimpleNamespace(user_data={})
    asyncio.run(app._show_search(update, context, query=query, page=0))
    callbacks = [
        button.callback_data
        for row in sent[0][1].inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert context.user_data["search_query"] == query
    assert max(len(value.encode("utf-8")) for value in callbacks) <= 64
    assert "searchpage_1" in callbacks


def test_callback_backed_commands_render_without_callback_query(monkeypatch):
    replies = []

    class Message:
        async def reply_text(self, text, **kwargs):
            replies.append((text, kwargs))

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=5),
        effective_chat=SimpleNamespace(id=5),
        callback_query=None,
        message=Message(),
    )
    context = SimpleNamespace(bot=SimpleNamespace(), user_data={})

    monkeypatch.setattr(
        app,
        "get_user_quota",
        lambda _uid: {"used": 1, "limit": 5, "remaining": 4, "is_premium": False},
    )
    monkeypatch.setattr(app, "get_referral_count", lambda _uid: 2)
    monkeypatch.setattr(app, "get_referral_bonus_total", lambda _uid: 20)
    monkeypatch.setattr(app, "is_user_premium", lambda _uid: False)
    monkeypatch.setattr(
        app,
        "get_user_streak",
        lambda _uid: {"streak": 3, "best": 3, "bonus": 2},
    )
    from nodick.telegram import keyboards
    monkeypatch.setattr(keyboards, "account_keyboard", lambda _uid: "account-menu")
    asyncio.run(app.my_stats_callback(update, context))

    monkeypatch.setattr(
        app,
        "get_bot_setting",
        lambda key, default="": "" if key in {"payment_info", "payment_qr"} else default,
    )
    asyncio.run(app.get_premium_callback(update, context))

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(app, "_check_force_join", not_blocked)
    monkeypatch.setattr(app, "get_favorites", lambda *_args, **_kwargs: ([], 0))
    monkeypatch.setattr(app, "get_user_size_limit", lambda _uid: None)
    monkeypatch.setattr(app, "main_menu", lambda _uid: "main-menu")
    asyncio.run(app.show_favorites(update, context))

    assert len(replies) == 3
    assert "Your Stats" in replies[0][0]
    assert "Premium Access" in replies[1][0]
    assert "No saved videos" in replies[2][0]
