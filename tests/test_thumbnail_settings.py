import asyncio
import base64
import io
import struct
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler

from nodick.telegram import app, keyboards
from test_growth_reliability import _run_db_script


def png(size=(16, 16), mode="RGBA"):
    output = io.BytesIO()
    Image.new(mode, size).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def store(monkeypatch):
    values = {}
    getter = lambda key, default="": values.get(key, default)
    monkeypatch.setattr(app, "get_bot_setting", getter)
    monkeypatch.setattr(keyboards, "get_bot_setting", getter)
    monkeypatch.setattr(app, "set_bot_setting", lambda key, value: values.__setitem__(key, value))
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    monkeypatch.setattr(app, "_replace_with_text", AsyncMock())
    return values


def callback(action, state=None):
    return (NS(callback_query=NS(data=action, answer=AsyncMock())),
            NS(user_data=dict(state or {})))


def upload(data, size=None):
    file = NS(file_size=size, download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    doc = NS(file_size=size, get_file=AsyncMock(return_value=file))
    msg = NS(document=doc, reply_text=AsyncMock())
    return NS(message=msg), file


def consume(update, context):
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(app.thumbnail_overlay_input(update, context))


def test_default_keyboard_grid_and_entry(store):
    rows = keyboards.thumbnail_keyboard().inline_keyboard
    assert [len(row) for row in rows] == [2, 2, 3, 2, 1]
    selected = [b.callback_data for row in rows for b in row if b.text.startswith("✅")]
    assert selected == ["thumbnail_style_pixelation", "thumbnail_strength_medium"]
    assert rows[-1][0].callback_data == "settings"
    assert any(b.callback_data == "thumbnail_menu" and "Thumbnail Style" in b.text
               for row in keyboards.settings_keyboard().inline_keyboard for b in row)
    assert all(len(b.callback_data.encode()) <= 64 for row in rows for b in row)


@pytest.mark.parametrize("kind,value", [("style", v) for v in ("pixelation", "blur", "solid", "overlay")]
                         + [("strength", v) for v in ("low", "medium", "high")])
def test_settings_save_immediately_and_mark_selected(store, kind, value):
    store["thumbnail_overlay_png"] = base64.b64encode(png()).decode()
    update, context = callback(f"thumbnail_{kind}_{value}", {"unrelated": 42, "waiting_for_search": True})
    asyncio.run(app.thumbnail_settings(update, context))
    assert store[f"thumbnail_{kind}"] == value
    assert context.user_data == {"unrelated": 42, "waiting_for_search": True}
    button = next(b for row in keyboards.thumbnail_keyboard().inline_keyboard
                  for b in row if b.callback_data == f"thumbnail_{kind}_{value}")
    assert button.text.startswith("✅")


@pytest.mark.parametrize("action", ["thumbnail_menu", "thumbnail_upload", "thumbnail_clear",
                                    "thumbnail_style_overlay", "thumbnail_strength_high"])
def test_callback_admin_gate(store, monkeypatch, action):
    monkeypatch.setattr(app, "_is_admin", lambda _: False)
    update, context = callback(action, {"unrelated": 9})
    asyncio.run(app.thumbnail_settings(update, context))
    assert store == {}
    assert context.user_data == {"unrelated": 9}
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
    app._replace_with_text.assert_not_awaited()


@pytest.mark.parametrize("raw", ["", "garbage", base64.b64encode(b"not PNG").decode()])
def test_overlay_selection_requires_valid_saved_png(store, raw):
    store.update(thumbnail_style="solid", thumbnail_overlay_png=raw)
    asyncio.run(app.thumbnail_settings(*callback("thumbnail_style_overlay")))
    assert store["thumbnail_style"] == "solid"
    assert "✅" not in app._replace_with_text.await_args.args[2]


def test_prompt_upload_normalizes_saves_selects_and_preserves_state(store):
    update, context = callback("thumbnail_upload", {"unrelated": "keep"})
    asyncio.run(app.thumbnail_settings(update, context))
    assert context.user_data["waiting_for_setting"] == "thumbnail_overlay"
    update, file = upload(png(mode="RGB"))
    consume(update, context)
    file.download_as_bytearray.assert_awaited_once()
    decoded = base64.b64decode(store["thumbnail_overlay_png"], validate=True)
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.mode == "RGBA" and image.size == (16, 16)
    assert store["thumbnail_style"] == "overlay"
    assert context.user_data == {"unrelated": "keep"}
    assert "✅" in update.message.reply_text.await_args.args[0]


@pytest.mark.parametrize("bad", [b"bad", png()[:-10], png((1025, 1)), png((1024, 1024)), b"x" * (512 * 1024 + 1)])
def test_invalid_upload_keeps_previous_and_waits_for_retry(store, bad):
    store.update(thumbnail_overlay_png="previous", thumbnail_style="solid")
    context = NS(user_data={"waiting_for_setting": "thumbnail_overlay", "unrelated": 5})
    update, _ = upload(bad)
    consume(update, context)
    assert store == {"thumbnail_overlay_png": "previous", "thumbnail_style": "solid"}
    assert context.user_data == {"waiting_for_setting": "thumbnail_overlay", "unrelated": 5}
    update, _ = upload(png())
    consume(update, context)
    assert store["thumbnail_style"] == "overlay"
    assert context.user_data == {"unrelated": 5}


def test_header_dimensions_checked_before_pillow_decode(store, monkeypatch):
    data = bytearray(png())
    data[16:24] = struct.pack(">II", 100000, 100000)
    decoder = Mock(side_effect=AssertionError("must not decode"))
    monkeypatch.setattr(Image, "open", decoder)
    with pytest.raises(ValueError):
        app._normalize_thumbnail_png(bytes(data))
    decoder.assert_not_called()


def test_normalized_output_limit(monkeypatch):
    original = Image.Image.save
    def save(image, output, *args, **kwargs):
        original(image, output, *args, **kwargs)
        output.write(b"x" * (512 * 1024))
    data = png()
    monkeypatch.setattr(Image.Image, "save", save)
    with pytest.raises(ValueError, match="Normalized"):
        app._normalize_thumbnail_png(data)


def test_transparency_preserved():
    data = app._normalize_thumbnail_png(png())
    with Image.open(io.BytesIO(data)) as image:
        assert image.getpixel((0, 0))[3] == 0


def test_telegram_size_rejected_before_download(store):
    update, file = upload(png(), size=512 * 1024 + 1)
    consume(update, NS(user_data={"waiting_for_setting": "thumbnail_overlay"}))
    update.message.document.get_file.assert_not_awaited()
    file.download_as_bytearray.assert_not_awaited()
    assert store == {}


def test_file_size_rejected_before_download(store):
    update, file = upload(png())
    file.file_size = 512 * 1024 + 1
    consume(update, NS(user_data={"waiting_for_setting": "thumbnail_overlay"}))
    file.download_as_bytearray.assert_not_awaited()


@pytest.mark.parametrize("admin", [True, False])
def test_non_document_and_unauthorized_never_save(store, monkeypatch, admin):
    monkeypatch.setattr(app, "_is_admin", lambda _: admin)
    context = NS(user_data={"waiting_for_setting": "thumbnail_overlay", "keep": True})
    consume(NS(message=NS(document=None, reply_text=AsyncMock())), context)
    assert store == {}
    assert context.user_data == {"waiting_for_setting": "thumbnail_overlay", "keep": True}


@pytest.mark.parametrize("state", [{}, {"waiting_for_setting": "broadcast"}, {"waiting_for_setting": "welcomegif"}])
def test_unrelated_input_is_not_claimed_or_changed(store, state):
    context = NS(user_data=dict(state))
    asyncio.run(app.thumbnail_overlay_input(NS(), context))
    assert context.user_data == state
    assert store == {}


@pytest.mark.parametrize("style,expected", [("overlay", "pixelation"), ("blur", "blur")])
def test_clear_overlay_and_back_to_settings(store, style, expected):
    store.update(thumbnail_overlay_png="old", thumbnail_style=style)
    update, context = callback("thumbnail_clear", {"waiting_for_setting": "thumbnail_overlay", "keep": True})
    asyncio.run(app.thumbnail_settings(update, context))
    assert store == {"thumbnail_overlay_png": "", "thumbnail_style": expected}
    assert context.user_data == {"keep": True}
    context.user_data["waiting_for_setting"] = "thumbnail_overlay"
    asyncio.run(app.settings_command(update, context))
    assert context.user_data == {"keep": True}


def test_cancel_aborts_upload_without_changing_saved_png(store, monkeypatch):
    monkeypatch.setattr(app, "main_menu", lambda _: None)
    store["thumbnail_overlay_png"] = "previous"
    context = NS(user_data={"waiting_for_setting": "thumbnail_overlay", "keep": True})
    update = NS(message=NS(reply_text=AsyncMock()), effective_user=NS(id=1))
    asyncio.run(app.cancel_command(update, context))
    assert context.user_data == {"keep": True}
    assert store == {"thumbnail_overlay_png": "previous"}


@pytest.mark.parametrize("action", ["thumbnail_style_blur", "thumbnail_strength_high", "thumbnail_clear"])
def test_failed_readback_never_reports_success(store, monkeypatch, action):
    monkeypatch.setattr(app, "set_bot_setting", lambda *_: None)
    asyncio.run(app.thumbnail_settings(*callback(action)))
    assert "✅" not in app._replace_with_text.await_args.args[2]


def test_upload_requires_png_readback_before_activation(store, monkeypatch):
    monkeypatch.setattr(app, "set_bot_setting", lambda *_: None)
    update, _ = upload(png())
    context = NS(user_data={"waiting_for_setting": "thumbnail_overlay"})
    consume(update, context)
    assert store == {}
    assert context.user_data["waiting_for_setting"] == "thumbnail_overlay"
    assert "✅" not in update.message.reply_text.await_args.args[0]


def test_actual_registration_order_and_filter_boundaries(store, monkeypatch):
    handlers = []
    application = NS(job_queue=None, add_handler=lambda handler, group=0: handlers.append((group, handler)),
                     add_error_handler=Mock())
    builder = Mock()
    builder.token.return_value.concurrent_updates.return_value.build.return_value = application
    monkeypatch.setattr(app, "Application", NS(builder=lambda: builder))
    monkeypatch.setattr(app, "init_db", lambda: None)
    app.build_application()
    group, overlay = next((g, h) for g, h in handlers if h.callback == app.thumbnail_overlay_input)
    assert group == -3
    assert next(g for g, h in handlers if h.callback == app.welcome_media_input) == -2
    assert next(g for g, h in handlers if h.callback == app.broadcast_input) == -1
    assert next(g for g, h in handlers if h.callback == app.handle_video) > group
    assert next(g for g, h in handlers if h.callback == app.handle_forward) > group
    cb_index = next(i for i, (_, h) in enumerate(handlers) if h.callback == app.thumbnail_settings)
    catch_index = next(i for i, (_, h) in enumerate(handlers) if h.callback == app._catchall_callback)
    assert cb_index < catch_index
    cb = handlers[cb_index][1]
    assert isinstance(cb, CallbackQueryHandler)
    for action in ("thumbnail_style_unknown", "thumbnail_strength_low_extra", "thumbnail_menu\n"):
        assert not cb.pattern.match(action)
    for chat_type, text, entities, expected in [
        ("private", "upload", [], True), ("group", "upload", [], False),
        ("private", "/cancel", [{"type": "bot_command", "offset": 0, "length": 7}], False),
    ]:
        update = Update.de_json({"update_id": 1, "message": {"message_id": 1, "date": 1,
                                "chat": {"id": 1, "type": chat_type}, "text": text, "entities": entities}}, None)
        assert bool(overlay.check_update(update)) is expected


def test_animated_png_rejected():
    output = io.BytesIO()
    Image.new("RGBA", (4, 4), "red").save(
        output, format="PNG", save_all=True,
        append_images=[Image.new("RGBA", (4, 4), "blue")], duration=100, loop=0,
    )
    with pytest.raises(ValueError, match="Static PNG"):
        app._normalize_thumbnail_png(output.getvalue())


def test_png_metadata_is_stripped():
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("comment", "uploaded metadata")
    output = io.BytesIO()
    Image.new("RGBA", (4, 4)).save(output, format="PNG", pnginfo=info)
    with Image.open(io.BytesIO(app._normalize_thumbnail_png(output.getvalue()))) as image:
        assert image.info == {}


def test_normalization_runs_off_event_loop(store, monkeypatch):
    worker = AsyncMock(wraps=asyncio.to_thread)
    monkeypatch.setattr(app.asyncio, "to_thread", worker)
    update, _ = upload(png())
    consume(update, NS(user_data={"waiting_for_setting": "thumbnail_overlay"}))
    assert worker.await_args.args[0] is app._normalize_thumbnail_png
    assert store["thumbnail_style"] == "overlay"


def test_unauthorized_settings_entry_is_safe(store, monkeypatch):
    monkeypatch.setattr(app, "_is_admin", lambda _: False)
    update, context = callback("settings", {"waiting_for_setting": "thumbnail_overlay", "keep": True})
    asyncio.run(app.settings_command(update, context))
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
    assert context.user_data == {"waiting_for_setting": "thumbnail_overlay", "keep": True}


def test_unauthorized_document_never_downloaded(store, monkeypatch):
    monkeypatch.setattr(app, "_is_admin", lambda _: False)
    update, file = upload(png())
    consume(update, NS(user_data={"waiting_for_setting": "thumbnail_overlay"}))
    update.message.document.get_file.assert_not_awaited()
    file.download_as_bytearray.assert_not_awaited()
    assert not store


def test_settings_storage_survives_restart(tmp_path):
    result = _run_db_script(tmp_path, '''
import json
from nodick import db
db.init_db()
initial = db.get_bot_setting('thumbnail_style', 'pixelation')
for key, value in [('thumbnail_style','overlay'), ('thumbnail_strength','high'), ('thumbnail_overlay_png','aGVsbG8=')]:
    db.set_bot_setting(key, value)
db.init_db()
saved = {key: db.get_bot_setting(key) for key in ['thumbnail_style','thumbnail_strength','thumbnail_overlay_png']}
db.set_bot_setting('thumbnail_overlay_png', '')
db.init_db()
print(json.dumps({'initial': initial, 'saved': saved, 'cleared': db.get_bot_setting('thumbnail_overlay_png')}))
''')
    assert result == {"initial": "pixelation", "saved": {"thumbnail_style": "overlay", "thumbnail_strength": "high",
                                                         "thumbnail_overlay_png": "aGVsbG8="}, "cleared": ""}
