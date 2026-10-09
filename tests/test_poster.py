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


def test_telegram_thumbnail_downloads_largest_preview_from_message(monkeypatch):
    source = _source_jpeg()
    message = SimpleNamespace(media=object())
    calls = {}

    class FakeClient:
        async def get_messages(self, chat_id, *, ids):
            calls.update(chat_id=chat_id, ids=ids)
            return [message]

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


def test_autoposter_sends_only_real_blurred_thumbnail(monkeypatch):
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
    assert "start=vid_42" in sent[0]["reply_markup"].inline_keyboard[0][0].url


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
