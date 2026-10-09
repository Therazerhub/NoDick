from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram import InlineKeyboardMarkup

from nodick.telegram import app


def test_auto_delete_defaults_to_30_minutes(monkeypatch):
    monkeypatch.setattr(
        app,
        "get_bot_setting",
        lambda key, default=None: {
            "auto_delete_enabled": "1",
            "auto_delete_minutes": "30",
        }.get(key, default),
    )
    assert app._auto_delete_minutes() == 30


def test_auto_delete_can_be_disabled_and_bad_timer_falls_back(monkeypatch):
    monkeypatch.setattr(
        app,
        "get_bot_setting",
        lambda key, default=None: "0" if key == "auto_delete_enabled" else default,
    )
    assert app._auto_delete_minutes() is None

    monkeypatch.setattr(
        app,
        "get_bot_setting",
        lambda key, default=None: {
            "auto_delete_enabled": "1",
            "auto_delete_minutes": "not-a-number",
        }.get(key, default),
    )
    assert app._auto_delete_minutes() == 30


def test_caption_warns_user_to_save_before_30_minute_delete():
    caption = app._append_auto_delete_notice("🎬 *Video title*", 30)
    assert "Save it before it disappears" in caption
    assert "auto-deletes in *30 minutes*" in caption


def test_message_id_normalization_handles_all_ptb_send_results():
    assert app._message_id_from_send_result(101) == 101
    assert app._message_id_from_send_result(SimpleNamespace(message_id=102)) == 102
    nested = SimpleNamespace(message_id=SimpleNamespace(message_id=103))
    assert app._message_id_from_send_result(nested) == 103
    assert app._message_id_from_send_result(None) is None


def test_schedule_uses_exact_30_minute_delay_and_message_id():
    scheduled = []

    class FakeJobQueue:
        def run_once(self, callback, **kwargs):
            scheduled.append((callback, kwargs))

    context = SimpleNamespace(job_queue=FakeJobQueue())
    sent = SimpleNamespace(message_id=777)
    assert app._schedule_auto_delete(context, -1009, sent, 30) is True
    assert len(scheduled) == 1
    callback, job = scheduled[0]
    assert callback is app._auto_delete_message
    assert job == {
        "when": 1800,
        "data": {"chat_id": -1009, "message_id": 777},
        "name": "autodel_-1009_777",
    }


def test_schedule_refuses_missing_message_id_or_job_queue():
    assert app._schedule_auto_delete(SimpleNamespace(job_queue=object()), 1, None, 30) is False
    assert app._schedule_auto_delete(SimpleNamespace(job_queue=None), 1, 99, 30) is False


def test_auto_delete_job_calls_telegram_delete_message():
    deleted = []

    class FakeBot:
        async def delete_message(self, **kwargs):
            deleted.append(kwargs)

    context = SimpleNamespace(
        bot=FakeBot(),
        job=SimpleNamespace(data={"chat_id": -1009, "message_id": 777}),
    )
    asyncio.run(app._auto_delete_message(context))
    assert deleted == [{"chat_id": -1009, "message_id": 777}]


def test_send_video_ref_returns_send_video_message_for_scheduler():
    returned = SimpleNamespace(message_id=501)
    calls = []

    class FakeBot:
        async def send_video(self, **kwargs):
            calls.append(kwargs)
            return returned

    result = asyncio.run(
        app._send_video_ref(FakeBot(), 123, "telegram-file-id", "caption", "keyboard")
    )
    assert result is returned
    assert calls[0]["chat_id"] == 123
    assert calls[0]["caption"] == "caption"


def test_send_video_ref_returns_copy_message_id_for_scheduler():
    returned = SimpleNamespace(message_id=502)
    calls = []

    class FakeBot:
        async def copy_message(self, **kwargs):
            calls.append(kwargs)
            return returned

    result = asyncio.run(
        app._send_video_ref(
            FakeBot(), 123, "channel_ref:-10055:99", "caption", "keyboard"
        )
    )
    assert result is returned
    assert calls[0]["from_chat_id"] == -10055
    assert calls[0]["message_id"] == 99


def test_enrich_send_adds_notice_and_schedules_same_30_minute_timer(monkeypatch):
    row = {
        "id": 42,
        "title": "Example Video",
        "file_id": "telegram-file-id",
        "duration": 120,
        "view_count": 3,
        "tags": "",
        "file_size": 1024,
    }
    sent_captions = []
    scheduled = []

    monkeypatch.setattr(app, "get_video", lambda _video_id: row)
    monkeypatch.setattr(app, "find_sibling_parts", lambda *_args: [])
    monkeypatch.setattr(app, "increment_view", lambda _video_id: None)
    monkeypatch.setattr(
        app,
        "bump_streak",
        lambda _user_id: {"new_record": False, "streak": 1, "bonus": 0},
    )
    monkeypatch.setattr(app, "get_video_metadata", lambda _video_id: None)
    monkeypatch.setattr(app, "_stash_available", False)
    monkeypatch.setattr(app, "_is_admin", lambda _update: False)
    monkeypatch.setattr(app, "_auto_delete_minutes", lambda: 30)
    monkeypatch.setattr(
        app,
        "video_actions",
        lambda *_args, **_kwargs: InlineKeyboardMarkup([]),
    )
    monkeypatch.setattr(app, "part_nav_row", lambda *_args: None)
    monkeypatch.setattr(app, "mark_welcome_conversion", lambda _user_id: None)

    async def fake_send(_bot, _chat_id, _file_ref, caption, _markup):
        sent_captions.append(caption)
        return SimpleNamespace(message_id=900)

    async def no_ad(_update, _context):
        return None

    monkeypatch.setattr(app, "_send_video_ref", fake_send)
    monkeypatch.setattr(app, "_maybe_send_ad", no_ad)

    class FakeJobQueue:
        def run_once(self, callback, **kwargs):
            scheduled.append((callback, kwargs))

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=None,
    )
    context = SimpleNamespace(
        bot=object(),
        bot_data={"bot_username": "Moyechan_bot"},
        user_data={},
        job_queue=FakeJobQueue(),
    )

    asyncio.run(app._enrich_and_send(update, context, 42))

    assert len(sent_captions) == 1
    assert "Save it before it disappears" in sent_captions[0]
    assert "auto-deletes in *30 minutes*" in sent_captions[0]
    assert scheduled[0][1]["when"] == 1800
    assert scheduled[0][1]["data"] == {"chat_id": 456, "message_id": 900}
