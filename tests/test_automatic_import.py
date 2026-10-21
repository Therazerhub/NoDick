from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nodick.services import automatic_import as auto


def setup_poll(monkeypatch, latest=450, channel=-1001):
    state = {"scan_channel_id": str(channel)}
    monkeypatch.setattr(auto, "get_bot_setting", lambda key, default="": state.get(key, default))
    monkeypatch.setattr(auto, "set_bot_setting", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(auto, "settings", SimpleNamespace(telegram_api_id=1, telegram_api_hash="hash", default_import_channel=""))

    class Client:
        async def get_messages(self, channel_id, *, limit):
            assert channel_id == int(state["scan_channel_id"])
            return [SimpleNamespace(id=latest)]

    async def client():
        return Client()

    monkeypatch.setattr(auto, "_get_telethon_client", client)
    return state


def test_first_tick_baselines_latest_and_repeated_tick_dedupes(monkeypatch):
    state = setup_poll(monkeypatch)
    calls = []

    class Importer:
        async def import_channel(self, channel_id, high, *, min_message_id):
            calls.append((channel_id, high, min_message_id))
            return {"status": "done", "total_checked": 1, "saved": 1}

    monkeypatch.setattr(auto, "MessageIDImporter", Importer)
    result = asyncio.run(auto.poll_new_videos())
    assert result["cursor"] == 450
    assert calls == [(-1001, 450, 450)]
    assert state["auto_import_cursor_-1001"] == "450"
    assert asyncio.run(auto.poll_new_videos())["status"] == "current"
    assert len(calls) == 1


def test_backlog_bounded_checkpoint_and_retry_failure(monkeypatch):
    state = setup_poll(monkeypatch, latest=850)
    state["auto_import_cursor_-1001"] = "100"
    calls = []
    fail = [True]

    class Importer:
        async def import_channel(self, channel_id, high, *, min_message_id):
            calls.append((min_message_id, high))
            if min_message_id == 201 and fail[0]:
                return {"status": "failed"}
            return {"status": "done", "total_checked": high - min_message_id + 1, "saved": 0}

    monkeypatch.setattr(auto, "MessageIDImporter", Importer)
    try:
        asyncio.run(auto.poll_new_videos())
        assert False, "expected failure"
    except RuntimeError:
        pass
    assert state["auto_import_cursor_-1001"] == "200"
    fail[0] = False
    result = asyncio.run(auto.poll_new_videos())
    assert result["checked"] == 300
    assert result["cursor"] == 500
    assert result["backlog"] is True
    assert calls == [(101, 200), (201, 300), (201, 300), (301, 400), (401, 500)]


def test_channel_change_during_fetch_does_not_advance_cursor(monkeypatch):
    state = setup_poll(monkeypatch)

    class Importer:
        async def import_channel(self, *_args, **_kwargs):
            state["scan_channel_id"] = "-1002"
            return {"status": "done", "total_checked": 1, "saved": 1}

    monkeypatch.setattr(auto, "MessageIDImporter", Importer)
    assert asyncio.run(auto.poll_new_videos())["status"] == "channel_changed"
    assert state["auto_import_cursor_-1001"] == "449"
