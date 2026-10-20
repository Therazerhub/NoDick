"""Sponsor UI contract tests; no provider requests or Telegram network calls."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler

from nodick.telegram import app, keyboards, sponsor

def test_video_document_is_rejected_without_replacing_tutorial(store, monkeypatch):
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    store["shortener_tutorial_video"] = "previous-video"
    update, context = shape()
    context.user_data["waiting_for_setting"] = "sponsor_tutorial"
    update.message.document = NS(file_id="document-id", mime_type="video/mp4")
    with pytest.raises(ApplicationHandlerStop):
        run(sponsor.tutorial_input(update, context))
    assert store["shortener_tutorial_video"] == "previous-video"
    context.bot.send_video.assert_not_awaited()


def test_invalid_video_delivery_does_not_replace_tutorial(store, monkeypatch):
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    store["shortener_tutorial_video"] = "previous-video"
    update, context = shape()
    context.user_data["waiting_for_setting"] = "sponsor_tutorial"
    update.message.video = NS(file_id="bad-video-id")
    context.bot.send_video = AsyncMock(side_effect=RuntimeError("Cannot send video"))
    with pytest.raises(ApplicationHandlerStop):
        run(sponsor.tutorial_input(update, context))
    assert store["shortener_tutorial_video"] == "previous-video"
    assert context.user_data["waiting_for_setting"] == "sponsor_tutorial"


REAL_JOIN_GATE = app._check_force_join


def run(awaitable):
    return asyncio.run(awaitable)


def shape(callback=False, action="sponsor_pass", chat="private"):
    msg = NS(reply_text=AsyncMock(), video=None, document=None)
    q = NS(data=action, answer=AsyncMock(), message=msg) if callback else None
    update = NS(message=None if callback else msg, callback_query=q,
                effective_chat=NS(id=10, type=chat), effective_user=NS(id=42))
    context = NS(user_data={}, args=[], bot=NS(username="test_bot", send_video=AsyncMock(), send_photo=AsyncMock()))
    return update, context


@pytest.fixture
def store(monkeypatch):
    values = {}
    getter = lambda key, default="": values.get(key, default)
    for module in (app, keyboards, sponsor):
        monkeypatch.setattr(module, "get_bot_setting", getter)
    monkeypatch.setattr(sponsor, "set_bot_setting", lambda k, v: values.__setitem__(k, v))
    monkeypatch.setattr(sponsor.shortener, "configured", lambda: True)
    monkeypatch.setattr(app, "_replace_with_text", AsyncMock())
    monkeypatch.setattr(app, "_check_force_join", AsyncMock(return_value=False))
    monkeypatch.setattr(app, "_is_admin", lambda _: False)
    monkeypatch.setattr(keyboards, "is_user_premium", lambda _: False)
    return values


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize("enabled", [True, False])
def test_menu_and_upsell_visibility(store, monkeypatch, enabled):
    monkeypatch.setattr(sponsor.shortener, "configured", lambda: enabled)
    for markup in (keyboards.main_menu(42), sponsor.upsell_keyboard()):
        assert any(b.callback_data == "sponsor_pass" for b in buttons(markup)) is enabled
    assert any(b.callback_data == "sponsor_settings" for b in buttons(keyboards.settings_keyboard()))


@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("status", ["ready", "active", "disabled", "error"])
def test_pass_command_and_callback(store, monkeypatch, callback, status):
    prepare = AsyncMock(return_value={"status": status, "shortened_url": "https://short.example/opaque", "expires_at": 2000, "premium_until": 2000000000})
    monkeypatch.setattr(sponsor.shortener, "prepare_premium_pass", prepare)
    update, context = shape(callback)
    run(sponsor.show_pass(update, context))
    prepare.assert_awaited_once_with(42, "test_bot")
    args = app._replace_with_text.await_args if callback else update.message.reply_text.await_args
    markup = args.args[3] if callback else args.kwargs["reply_markup"]
    text = args.args[2] if callback else args.args[0]
    urls = [b.url for b in buttons(markup) if b.url]
    assert urls == (["https://short.example/opaque"] if status == "ready" else [])
    assert "t.me/" not in text
    assert any(b.callback_data == "sponsor_tutorial" for b in buttons(markup))
    if status == "ready":
        assert "30 minutes" in text and "24 hours" in text
    if status == "active":
        assert "Premium is active" in text


@pytest.mark.parametrize("chat", ["group", "supergroup", "channel"])
def test_private_only(store, monkeypatch, chat):
    prepare, redeem = AsyncMock(), AsyncMock()
    monkeypatch.setattr(sponsor.shortener, "prepare_premium_pass", prepare)
    monkeypatch.setattr(sponsor.shortener, "redeem_premium_pass", redeem)
    update, context = shape(chat=chat)
    run(sponsor.show_pass(update, context))
    run(sponsor.redeem(update, context, "a" * 32))
    prepare.assert_not_awaited()
    redeem.assert_not_awaited()


@pytest.mark.parametrize("payload", ["pass_short", "pass_" + "a" * 33, "pass_" + "a" * 31 + "!", "pass_" + "a" * 32 + "\n"])
def test_malformed_pass_never_falls_through(store, monkeypatch, payload):
    redeem = AsyncMock()
    monkeypatch.setattr(sponsor.shortener, "redeem_premium_pass", redeem)
    welcome = AsyncMock()
    monkeypatch.setattr(app, "_send_welcome", welcome)
    update, context = shape()
    context.args = [payload]
    run(app.start(update, context))
    redeem.assert_not_awaited()
    welcome.assert_not_awaited()
    text = update.message.reply_text.await_args.args[0]
    assert "invalid" in text and payload not in text


@pytest.mark.parametrize("status", ["activated", "active", "expired", "invalid", "disabled"])
def test_start_redemption_status_and_no_token_echo(store, monkeypatch, status):
    token = "aB_-" * 8
    redeem = AsyncMock(return_value={"status": status, "premium_until": 2000000000})
    monkeypatch.setattr(sponsor.shortener, "redeem_premium_pass", redeem)
    update, context = shape()
    context.args = ["pass_" + token]
    run(app.start(update, context))
    redeem.assert_awaited_once_with(42, token)
    app._check_force_join.assert_awaited_once_with(update, context, pending_action="pass:" + token)
    assert token not in str(update.message.reply_text.await_args)
    assert "t.me/" not in str(update.message.reply_text.await_args)


def test_forcejoin_preserves_exact_pass_until_membership_success(store, monkeypatch):
    token = "Ab_-" * 8
    update, context = shape(True, "verify_join")
    context.user_data["pending_after_join"] = "pass:" + token
    monkeypatch.setattr(app, "_force_join_config", lambda: {"enabled": True, "valid": True, "channels": [{"id": -123, "name": "Channel"}]})
    monkeypatch.setattr(app, "record_growth_event", lambda *a, **kw: None)
    context.bot.get_chat_member = AsyncMock(return_value=NS(status="left"))
    redeem = AsyncMock(return_value={"status": "activated", "premium_until": 2000000000})
    monkeypatch.setattr(sponsor.shortener, "redeem_premium_pass", redeem)
    run(app.verify_join_callback(update, context))
    assert context.user_data["pending_after_join"] == "pass:" + token
    redeem.assert_not_awaited()
    context.bot.get_chat_member.return_value = NS(status="member")
    run(app.verify_join_callback(update, context))
    redeem.assert_awaited_once_with(42, token)
    assert "pending_after_join" not in context.user_data


def test_initial_pass_join_gate_stops_redemption(store, monkeypatch):
    token = "a" * 32
    async def gate(update, context, pending_action=None):
        context.user_data["pending_after_join"] = pending_action
        return True
    monkeypatch.setattr(app, "_check_force_join", gate)
    redeem = AsyncMock()
    monkeypatch.setattr(sponsor.shortener, "redeem_premium_pass", redeem)
    update, context = shape()
    context.args = ["pass_" + token]
    run(app.start(update, context))
    assert context.user_data["pending_after_join"] == "pass:" + token
    redeem.assert_not_awaited()


def test_real_join_gate_retains_token_without_rendering_it(store, monkeypatch):
    monkeypatch.setattr(app, "_check_force_join", REAL_JOIN_GATE)
    monkeypatch.setattr(app, "_force_join_config", lambda: {
        "enabled": True, "valid": True,
        "channels": [{"id": -123, "name": "Channel", "url": "https://t.me/channel"}],
    })
    monkeypatch.setattr(app, "record_growth_event", lambda *a, **kw: None)
    token = "A_-b" * 8
    update, context = shape()
    context.bot.get_chat_member = AsyncMock(return_value=NS(status="left"))
    context.args = ["pass_" + token]
    redeem = AsyncMock()
    monkeypatch.setattr(sponsor.shortener, "redeem_premium_pass", redeem)
    run(app.start(update, context))
    redeem.assert_not_awaited()
    assert context.user_data["pending_after_join"] == "pass:" + token
    assert token not in str(update.message.reply_text.await_args)


@pytest.mark.parametrize("pending", ["broadcast", "sponsor_tutorial"])
def test_cancel_preserves_forcejoin_and_unrelated_state(store, pending):
    update, context = shape()
    context.user_data.update(waiting_for_setting=pending, unrelated=9, pending_after_join="pass:" + "a" * 32)
    run(app.cancel_command(update, context))
    assert "waiting_for_setting" not in context.user_data
    assert context.user_data["unrelated"] == 9
    assert context.user_data["pending_after_join"] == "pass:" + "a" * 32


@pytest.mark.parametrize("action", ["sponsor_settings", "sponsor_toggle", "sponsor_upload", "sponsor_clear"])
def test_admin_callbacks_denied(store, action):
    update, context = shape(True, action)
    context.user_data["unrelated"] = 1
    run(sponsor.admin_settings(update, context))
    assert not store
    assert context.user_data == {"unrelated": 1}
    app._replace_with_text.assert_not_awaited()
    assert update.callback_query.answer.await_args.kwargs["show_alert"]


def test_admin_toggle_clear_and_upload_prompt(store, monkeypatch):
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    update, context = shape(True, "sponsor_toggle")
    run(sponsor.admin_settings(update, context))
    assert store["shortener_enabled"] == "0"
    run(sponsor.admin_settings(update, context))
    assert store["shortener_enabled"] == "1"
    update.callback_query.data = "sponsor_upload"
    run(sponsor.admin_settings(update, context))
    assert context.user_data["waiting_for_setting"] == "sponsor_tutorial"
    update.callback_query.data = "sponsor_clear"
    run(sponsor.admin_settings(update, context))
    assert store["shortener_tutorial_video"] == ""
    assert "waiting_for_setting" not in context.user_data


@pytest.mark.parametrize("kind", ["video"])
def test_upload_exact_file_id_saved_and_read_back(store, monkeypatch, kind):
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    update, context = shape()
    context.user_data.update(waiting_for_setting="sponsor_tutorial", unrelated=9)
    setattr(update.message, kind, NS(file_id="EXACT_file_id-123", mime_type="video/mp4"))
    with pytest.raises(ApplicationHandlerStop):
        run(sponsor.tutorial_input(update, context))
    assert store["shortener_tutorial_video"] == "EXACT_file_id-123"
    assert context.user_data == {"unrelated": 9}
    assert "saved" in update.message.reply_text.await_args.args[0]


@pytest.mark.parametrize("admin,valid", [(False, True), (True, False)])
def test_upload_rejects_unauthorized_or_invalid(store, monkeypatch, admin, valid):
    monkeypatch.setattr(app, "_is_admin", lambda _: admin)
    update, context = shape()
    context.user_data.update(waiting_for_setting="sponsor_tutorial", unrelated=9)
    if valid:
        update.message.video = NS(file_id="not_saved")
    with pytest.raises(ApplicationHandlerStop):
        run(sponsor.tutorial_input(update, context))
    assert not store
    assert context.user_data["unrelated"] == 9
    if admin:
        assert context.user_data["waiting_for_setting"] == "sponsor_tutorial"


def test_upload_must_verify_persistence(store, monkeypatch):
    monkeypatch.setattr(app, "_is_admin", lambda _: True)
    monkeypatch.setattr(sponsor, "set_bot_setting", lambda *args: None)
    update, context = shape()
    update.message.video = NS(file_id="new")
    context.user_data["waiting_for_setting"] = "sponsor_tutorial"
    with pytest.raises(ApplicationHandlerStop):
        run(sponsor.tutorial_input(update, context))
    assert "sponsor_tutorial" == context.user_data["waiting_for_setting"]
    assert "Could not save" in update.message.reply_text.await_args.args[0]


@pytest.mark.parametrize("state", ["welcomegif", "broadcast", "thumbnail_overlay", None])
def test_upload_leaves_other_states_untouched(store, state):
    update, context = shape()
    context.user_data["waiting_for_setting"] = state
    run(sponsor.tutorial_input(update, context))
    assert context.user_data == {"waiting_for_setting": state}
    update.message.reply_text.assert_not_awaited()


def test_tutorial_env_fallback_explicit_clear_and_video(store, monkeypatch):
    monkeypatch.setattr(sponsor, "settings", NS(shortener_tutorial_video="env-file-id", root=Path("/bot")))
    update, context = shape(True, "sponsor_tutorial")
    run(sponsor.tutorial(update, context))
    assert context.bot.send_video.await_args.kwargs["video"] == "env-file-id"
    store["shortener_tutorial_video"] = "stored-file-id"
    run(sponsor.tutorial(update, context))
    assert context.bot.send_video.await_args.kwargs["video"] == "stored-file-id"
    store["shortener_tutorial_video"] = ""
    context.bot.send_video.reset_mock()
    run(sponsor.tutorial(update, context))
    assert context.bot.send_video.await_args.kwargs["video"] == Path("/bot/assets/shortener-tutorial.mp4")
    assert context.bot.send_video.await_args.kwargs["caption"] == "General walkthrough — individual provider pages may differ."


def test_tutorial_defaults_to_bundled_file(store, monkeypatch):
    monkeypatch.setattr(sponsor, "settings", NS(shortener_tutorial_video="", root=Path("/bot")))
    update, context = shape(True, "sponsor_tutorial")
    run(sponsor.tutorial(update, context))
    assert context.bot.send_video.await_args.kwargs["video"] == Path("/bot/assets/shortener-tutorial.mp4")


def test_real_registration_and_upload_priority(store, monkeypatch):
    monkeypatch.setattr(app, "init_db", lambda: None)
    monkeypatch.setattr(app, "settings", NS(bot_token="123456:test"))
    application = app.build_application()
    handlers = application.handlers[0]
    command = next(h for h in handlers if isinstance(h, CommandHandler) and "pass" in h.commands)
    assert command.commands == frozenset({"pass", "daypremium"})
    assert command.callback is sponsor.show_pass
    expected = {"sponsor_pass": sponsor.show_pass, "sponsor_tutorial": sponsor.tutorial,
                **{f"sponsor_{a}": sponsor.admin_settings for a in ["settings", "toggle", "upload", "clear"]}}
    for data, callback in expected.items():
        handler = next(h for h in handlers if isinstance(h, CallbackQueryHandler) and (h.pattern is None or h.pattern.match(data)))
        assert handler.callback is callback
    upload = application.handlers[-4][0]
    assert upload.callback is sponsor.tutorial_input
    assert -4 < -3 < -2 < -1
    def incoming(chat_type="private", command=False):
        msg = {"message_id": 1, "date": 1, "chat": {"id": 42, "type": chat_type}, "text": "/cancel" if command else "upload"}
        if command:
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": 7}]
        return Update.de_json({"update_id": 1, "message": msg}, application.bot)
    assert upload.check_update(incoming())
    assert not upload.check_update(incoming("group"))
    assert not upload.check_update(incoming(command=True))
