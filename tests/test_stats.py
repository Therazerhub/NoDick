"""Regression coverage for the main bot's Stats command and button."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nodick import db
from nodick.telegram import app
from test_growth_reliability import _run_db_script


def test_postgres_retention_uses_activity_cte_column(monkeypatch):
    """Exercise the PG branch: activity exposes activity_date, not created_at."""
    queries = []

    def fetchone(sql, *args):
        queries.append(sql)
        if "WITH cohorts" in sql:
            assert "AS activity_date" in sql
            assert "a.created_at" not in sql
            assert "a.activity_date=c.cohort_date+1" in sql
            assert "a.activity_date=c.cohort_date+7" in sql
            return dict(d1=1, d1_eligible=2, d7=1, d7_eligible=2)
        return {"c": 1}

    monkeypatch.setattr(db, "_using_pg", True)
    monkeypatch.setattr(db, "_fetchall", lambda *args: [])
    monkeypatch.setattr(db, "_fetchone", fetchone)
    metrics = db.get_growth_metrics(30)
    assert len(queries) == 3
    assert metrics["retention"] == dict(d1=1, d1_eligible=2, d7=1, d7_eligible=2)


def test_sqlite_stats_fresh_and_repeated_initialization(tmp_path):
    result = _run_db_script(tmp_path, """
import json
from nodick import db
db.init_db()
db.init_db()
empty = db.get_growth_metrics()
for uid in (101, 102):
    db.record_growth_event('start', uid, source='organic')
db._execute("UPDATE growth_events SET created_at=datetime('now','-8 days')")
for days in (7, 1):
    db.record_growth_event('delivery', 101)
    db._execute("UPDATE growth_events SET created_at=datetime('now', ?) WHERE id=(SELECT MAX(id) FROM growth_events)", (f'-{days} days',))
metrics = db.get_growth_metrics()
print(json.dumps({'empty': empty['retention'], 'retention': metrics['retention'], 'experiment': db.get_welcome_experiment_stats()}))
""")
    assert result["empty"] == dict(d1=0, d1_eligible=0, d7=0, d7_eligible=0)
    assert result["retention"] == dict(d1=1, d1_eligible=2, d7=1, d7_eligible=2)
    assert set(result["experiment"]) == {"A", "B"}


@pytest.mark.parametrize("callback", [False, True])
def test_stats_command_and_button_render(monkeypatch, callback):
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(message_id=10)) if callback else None
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=101), message=message)
    replace = AsyncMock()
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    monkeypatch.setattr(app, "_check_force_join", AsyncMock(return_value=False))
    monkeypatch.setattr(app, "_replace_with_text", replace)
    for name, value in [("db_video_count", 12), ("total_views", 50), ("category_count", 3), ("get_user_count", 2)]:
        monkeypatch.setattr(app, name, lambda value=value: value)
    monkeypatch.setattr(app, "get_welcome_experiment_stats", lambda: {v: dict(converted=0, exposed=0) for v in ("A", "B")})
    monkeypatch.setattr(app, "get_growth_metrics", lambda _: dict(retention=dict(d1=0, d1_eligible=0, d7=0, d7_eligible=0), funnel={}, sources=[], dau=0, wau=0))
    monkeypatch.setattr(app, "main_menu", lambda _: None)
    asyncio.run(app.stats(update, SimpleNamespace()))
    if callback:
        query.answer.assert_awaited_once()
        replace.assert_awaited_once()
        text = replace.await_args.args[2]
    else:
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
    assert "NoDick Stats" in text
    assert "Videos: 12" in text
    assert "D1: 0/0" in text
