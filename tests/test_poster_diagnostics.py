"""Local diagnostics tests: no Telegram, external metadata, or real DB writes."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nodick.services import poster, thumbnail_safety


@pytest.fixture
def store(monkeypatch):
    values = {}
    monkeypatch.setattr(poster, "get_bot_setting", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(poster, "set_bot_setting", values.__setitem__)
    return values


def result(store):
    return json.loads(store["auto_post_last_result"])


@pytest.fixture
def ready(monkeypatch, store):
    store["auto_post_channels"] = '[{"id": -1001}, {"id": "-1002"}]'
    monkeypatch.setattr(poster, "_select_video", lambda: {"id": 42, "title": "Example"})
    monkeypatch.setattr(poster, "_fetch_teaser_source", AsyncMock(return_value=(b"source", "telegram")))
    monkeypatch.setattr(thumbnail_safety, "create_censored_thumbnail", AsyncMock(return_value=b"rendered"))
    return SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="ExampleBot")), send_photo=AsyncMock())


@pytest.mark.parametrize("failures,status,count", [(0, "sent", 2), (1, "partial", 1), (2, "failed", 0)])
def test_delivery_counts_and_status(store, ready, failures, status, count):
    ready.send_photo.side_effect = [RuntimeError("secret://credential")] * failures + [None] * (2 - failures)
    assert asyncio.run(poster.send_auto_post(ready)) == count
    saved = result(store)
    assert saved["status"] == status
    assert saved["successes"] == count
    assert saved["failed"] == failures
    assert saved["video_id"] == 42
    assert "secret" not in store["auto_post_last_result"]
    assert ("auto_post_last" in store) is bool(count)
    if count:
        assert datetime.fromisoformat(store["auto_post_last"]).utcoffset() == timedelta(0)


@pytest.mark.parametrize("case", ["cooldown", "no_channels", "no_videos", "no_thumbnail", "render_failed", "missing_username"])
def test_preconditions_report_exact_reason(monkeypatch, store, ready, case):
    status = case
    if case == "cooldown":
        store["auto_post_last"] = datetime.now(timezone.utc).isoformat()
    elif case == "no_channels":
        store["auto_post_channels"] = "[]"
    elif case == "no_videos":
        monkeypatch.setattr(poster, "_select_video", lambda: None)
    elif case == "no_thumbnail":
        status = "no_source"
        monkeypatch.setattr(poster, "_fetch_teaser_source", AsyncMock(return_value=(None, "none")))
    elif case == "render_failed":
        status = "no_render"
        monkeypatch.setattr(thumbnail_safety, "create_censored_thumbnail", AsyncMock(return_value=b""))
    elif case == "missing_username":
        ready.get_me.return_value.username = None
    assert asyncio.run(poster.send_auto_post(ready)) == 0
    saved = result(store)
    assert saved["status"] == status
    assert saved["reason"] == poster.POST_RESULT_REASONS[status]
    assert saved["successes"] == saved["failed"] == 0
    ready.send_photo.assert_not_awaited()


def test_force_bypasses_cooldown(store, ready):
    store["auto_post_last"] = datetime.now(timezone.utc).isoformat()
    assert asyncio.run(poster.send_auto_post(ready, force=True)) == 2
    assert result(store)["status"] == "sent"


@pytest.mark.parametrize("hours,expected", [("bad", 21600), (None, 21600), ("", 21600), ("-9", 0), ("0", 0), ("999", 604800), ("2", 7200)])
def test_cooldown_configuration_clamps_and_defaults(store, hours, expected):
    store.update(auto_post_last=datetime.now(timezone.utc).isoformat(), auto_post_cooldown=hours)
    remaining = poster.cooldown_seconds_remaining()
    assert type(remaining) is int
    assert expected - 1 <= remaining <= expected
    assert poster._cooldown_active() is (expected > 0)


@pytest.mark.parametrize("zone", [None, timezone.utc, timezone(timedelta(hours=5, minutes=30))])
def test_legacy_naive_and_aware_timestamps(store, zone):
    last = datetime.now(timezone.utc) - timedelta(hours=1)
    last = last.replace(tzinfo=None) if zone is None else last.astimezone(zone)
    store["auto_post_last"] = last.isoformat()
    assert 17999 <= poster.cooldown_seconds_remaining() <= 18000


@pytest.mark.parametrize("value", [None, "", "not a date", "9999-12-31T23:59:59+00:00", 15, "2001-01-01T00:00:00Z"])
def test_invalid_missing_expired_and_overflow_times(store, value):
    store["auto_post_last"] = value
    assert poster.cooldown_seconds_remaining() == 0


@pytest.mark.parametrize("raw", ['null', '{}', '"wrong"', '[null, 1, {"id": null}, {"id": true}, {"id": "bad"}, {"id": 0}]', 'broken'])
def test_bad_channels_fall_back_to_valid_legacy(store, raw):
    store.update(auto_post_channels=raw, auto_post_channel=-1009)
    assert poster._get_channels() == [{"id": "-1009", "name": "Channel -1009"}]
    store["auto_post_channel"] = "garbage"
    assert poster._get_channels() == []


def test_channel_ids_normalized_and_metadata_preserved(store):
    store["auto_post_channels"] = '[{"id": -1001, "name": "One", "url": "https://t.me/example"}, {"id": " @ExampleChannel "}, {"id": "invalid"}]'
    assert poster._get_channels() == [
        {"id": "-1001", "name": "One", "url": "https://t.me/example"},
        {"id": "@ExampleChannel"},
    ]


def test_manual_destination_filter(store, ready):
    assert asyncio.run(poster.send_auto_post(ready, channel_id="-1002")) == 1
    assert ready.send_photo.call_args.kwargs["chat_id"] == "-1002"


def test_result_safe_json_and_aware_utc(store):
    poster.record_post_result("error", "password=secret\nhttps://credentials", successes="2", failed="bad", video_id="42")
    saved = result(store)
    assert saved["reason"] == poster.POST_RESULT_REASONS["error"]
    assert saved["successes"] == 2 and saved["failed"] == 0 and saved["video_id"] == 42
    assert datetime.fromisoformat(saved["checked_at"]).utcoffset() == timedelta(0)
    poster.record_post_result("sent", RuntimeError("secret"), successes=float("inf"), failed=-1)
    assert result(store)["successes"] == result(store)["failed"] == 0
    assert "video_id" not in result(store)
    assert "secret" not in store["auto_post_last_result"]


@pytest.mark.parametrize("status", ["disabled", "error"])
def test_scheduler_results(store, status):
    poster.record_post_result(status, status)
    assert result(store)["status"] == status
    assert result(store)["reason"] == poster.POST_RESULT_REASONS[status]


def test_delivery_survives_all_setting_write_failures(monkeypatch, store, ready):
    def fail(*args):
        raise RuntimeError("credentials must not leak")
    monkeypatch.setattr(poster, "set_bot_setting", fail)
    assert asyncio.run(poster.send_auto_post(ready)) == 2
    assert ready.send_photo.await_count == 2


def test_result_write_failure_is_best_effort(monkeypatch, store):
    monkeypatch.setattr(poster, "set_bot_setting", lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
    assert poster.record_post_result("sent", "sent", 1) is None
