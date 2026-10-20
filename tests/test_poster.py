from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageStat

from nodick.services import message_importer, poster


def _source_jpeg(size: tuple[int, int] = (240, 135)) -> bytes:
    image = Image.new("RGB", size, "#14141c")
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle((0, 0, width // 2, height), fill="#c02d63")
    draw.ellipse((width // 3, height // 5, width - 8, height - 8), fill="#2d8ed1")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


def test_small_thumbnail_keeps_structure_after_blur():
    rendered = poster.create_blurred_thumbnail(_source_jpeg())
    assert rendered
    image = Image.open(io.BytesIO(rendered))
    assert image.size == (960, 540)
    assert image.format == "JPEG"
    average_stddev = sum(ImageStat.Stat(image).stddev) / 3
    assert average_stddev > 8, average_stddev


def test_invalid_image_never_becomes_a_postable_raw_payload():
    assert poster.create_blurred_thumbnail(b"not-an-image") == b""


def test_stash_fetch_chooses_largest_image_and_sets_user_agent(monkeypatch):
    source = _source_jpeg((640, 360))
    requested = {}

    monkeypatch.setattr(
        poster,
        "settings",
        SimpleNamespace(
            stash_configured=True,
            stashdb_graphql_url="https://stash.example/graphql",
            stashdb_api_key="test-only",
        ),
    )
    monkeypatch.setattr(
        poster,
        "query_api",
        lambda *_args, **_kwargs: {
            "data": {
                "findScene": {
                    "images": [
                        {"url": "https://img.example/small.jpg", "width": 200, "height": 100},
                        {"url": "https://img.example/large.jpg", "width": 1280, "height": 720},
                    ]
                }
            }
        },
    )

    def fake_get(url, *, timeout, headers):
        requested.update(url=url, timeout=timeout, headers=headers)
        return SimpleNamespace(status_code=200, content=source)

    monkeypatch.setattr(poster.requests, "get", fake_get)
    assert poster.fetch_scene_image("scene-1") == source
    assert requested["url"] == "https://img.example/large.jpg"
    assert requested["timeout"] == 10
    assert "NoDickPoster" in requested["headers"]["User-Agent"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_telegram_thumbnail_downloads_largest_preview_from_message(monkeypatch, wrapped):
    source = _source_jpeg()
    message = SimpleNamespace(media=object())
    calls = {}

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            calls.update(chat_id=chat_id, ids=ids)
            return [message] if wrapped else message

        async def download_media(self, media, *, file, thumb):
            calls.update(media=media, thumb=thumb)
            file.write(source)

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(message_importer, "_get_telethon_client", fake_client)
    result = asyncio.run(poster.fetch_telegram_thumb(
        {"id": 9, "source_chat_id": -1001234, "source_message_id": 88}
    ))
    assert result == source
    assert calls == {
        "chat_id": -1001234,
        "ids": 88,
        "media": message,
        "thumb": -1,
    }


def test_autoposter_sends_only_censored_thumbnail(monkeypatch):
    from unittest.mock import AsyncMock
    from nodick.services import thumbnail_safety
    monkeypatch.setattr(thumbnail_safety, "_detect", AsyncMock(return_value=[
        {"class": "BUTTOCKS_EXPOSED", "score": 0.9, "box": [50, 50, 20, 20]}
    ]))
    thumbnail_safety._cache.clear()
    monkeypatch.setattr(thumbnail_safety, "get_bot_setting", lambda key, default='': default)
    source = _source_jpeg()
    video = {
        "id": 42,
        "title": "Local title",
        "category": "Test",
        "stashdb_scene_id": "scene-42",
        "stashdb_title": "Matched title",
        "stashdb_performer": "Performer",
        "source_chat_id": -1001,
        "source_message_id": 10,
    }
    sent = []
    marked = []

    monkeypatch.setattr(poster, "_cooldown_active", lambda: False)
    monkeypatch.setattr(poster, "_get_channels", lambda: [{"id": "-1009", "name": "Test"}])
    monkeypatch.setattr(poster, "_select_video", lambda: video)
    monkeypatch.setattr(poster, "_mark_posted", marked.append)

    async def fake_source(_video):
        return source, "telegram"

    monkeypatch.setattr(poster, "_fetch_teaser_source", fake_source)

    class FakeBot:
        async def get_me(self):
            return SimpleNamespace(username="Moyechan_bot")

        async def send_photo(self, **kwargs):
            sent.append(kwargs)

    assert asyncio.run(poster.send_auto_post(FakeBot(), force=True)) == 1
    assert marked == [42]
    assert len(sent) == 1
    image = Image.open(io.BytesIO(sent[0]["photo"]))
    assert image.size == (960, 540)
    assert sum(ImageStat.Stat(image).stddev) / 3 > 8
    assert "start=vid_42" in sent[0]["reply_markup"].inline_keyboard[-1][0].url


def test_autoposter_aborts_instead_of_sending_gray_placeholder(monkeypatch):
    candidates = iter(
        {
            "id": video_id,
            "title": f"Video {video_id}",
            "category": "Test",
            "stashdb_scene_id": f"scene-{video_id}",
        }
        for video_id in range(1, 7)
    )
    monkeypatch.setattr(poster, "_cooldown_active", lambda: False)
    monkeypatch.setattr(poster, "_get_channels", lambda: [{"id": "-1009", "name": "Test"}])
    monkeypatch.setattr(poster, "_select_video", lambda: next(candidates, None))

    async def no_source(_video):
        return None, "none"

    monkeypatch.setattr(poster, "_fetch_teaser_source", no_source)

    class FakeBot:
        async def get_me(self):
            raise AssertionError("get_me must not be reached without a real image")

        async def send_photo(self, **_kwargs):
            raise AssertionError("gray placeholder must never be sent")

    assert asyncio.run(poster.send_auto_post(FakeBot(), force=True)) == 0


def test_stash_image_fetch_runs_off_event_loop(monkeypatch):
    import time

    def slow_fetch(_scene_id):
        time.sleep(0.08)
        return _source_jpeg()

    monkeypatch.setattr(poster, "fetch_scene_image", slow_fetch)

    async def exercise():
        task = asyncio.create_task(
            poster._fetch_teaser_source({"stashdb_scene_id": "scene-1"})
        )
        await asyncio.sleep(0.01)
        responsive = not task.done()
        result = await task
        return responsive, result

    responsive, result = asyncio.run(exercise())
    assert responsive is True
    assert result == (_source_jpeg(), "stashdb")


def test_autoposter_selection_uses_one_exclusion_query(monkeypatch):
    calls = []
    monkeypatch.setattr(
        poster,
        "get_bot_setting",
        lambda key, default="": "[1, 2, 3]" if key == "auto_post_posted_ids" else default,
    )

    def fake_fetch(sql, params=None):
        calls.append((sql, params))
        return {"id": 4, "title": "fresh"}

    monkeypatch.setattr(poster, "_fetchone", fake_fetch)
    picked = poster._select_video()
    assert picked["id"] == 4
    assert len(calls) == 1
    assert "NOT IN" in calls[0][0]
    assert calls[0][1] == (1, 2, 3)
