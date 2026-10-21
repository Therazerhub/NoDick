"""Private sponsor-pass UI; service owns eligibility and atomic redemption."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.ext import ApplicationHandlerStop

from nodick.config import settings
from nodick.db import get_bot_setting, set_bot_setting
from nodick.services import shortener

# Missing-setting sentinel preserves the DB getter's string contract while
# distinguishing a missing tutorial from an intentional empty override.
_UNSET = "__sponsor_setting_unset__"

PASS_PAYLOAD = re.compile(r"pass_([A-Za-z0-9_-]{32})", re.ASCII)
TOKEN = re.compile(r"[A-Za-z0-9_-]{32}", re.ASCII)
ADMIN_PATTERN = r"^sponsor_(settings|toggle|upload|clear)$"


def available() -> bool:
    return shortener.configured()


def tutorial_value() -> str | Path:
    # Empty DB values deliberately reset to the bundled walkthrough, whereas
    # absent DB settings may inherit the configured URL / Telegram file ID.
    value = get_bot_setting("shortener_tutorial_video", _UNSET)
    if value in (None, _UNSET):
        value = getattr(settings, "shortener_tutorial_video", "") or ""
    return str(value) if value else Path(settings.root) / "assets" / "shortener-tutorial.mp4"


def pass_keyboard(url=None):
    rows = []
    if url:
        rows.append([Button("🔗 Open Sponsor Link", url=url)])
    rows.append([Button("🎬 Tutorial", callback_data="sponsor_tutorial"),
                 Button("👑 Premium", callback_data="get_premium")])
    rows.append([Button("🔙 Back", callback_data="menu")])
    return Markup(rows)


def upsell_keyboard():
    rows = []
    if available():
        rows.append([Button("🎁 Get Full Access", callback_data="sponsor_pass")])
    rows.append([Button("🔙 Back", callback_data="menu")])
    return Markup(rows)


def _active_text(result):
    until = result.get("premium_until")
    expiry = ""
    if until:
        expiry = "\nUntil: " + datetime.fromtimestamp(int(until), timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    return "👑 Premium is active." + expiry + "\nEnjoy your Premium access — no sponsor link needed."


async def _show(update, context, text, markup=None):
    from nodick.telegram import app
    if update.callback_query:
        return await app._replace_with_text(update, context, text, markup)
    return await update.message.reply_text(text, reply_markup=markup)


async def _private(update, context):
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
        return True
    if update.callback_query:
        await update.callback_query.answer("Open a private chat with the bot to use this feature.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Open a private chat with the bot to use this feature.")
    return False


async def show_pass(update, context):
    from nodick.telegram import app
    if not await _private(update, context):
        return
    if await app._check_force_join(update, context, pending_action="sponsor_pass"):
        return
    if update.callback_query:
        await update.callback_query.answer()
    result = await shortener.prepare_premium_pass(update.effective_user.id, context.bot.username)
    status = result.get("status")
    if status == "active":
        text, url = _active_text(result), None
    elif status == "ready":
        text = ("🎁 Get Full Access\n\nOpen the sponsor link and follow its steps, then return to this bot using the final button.\n\n"
                "This link is valid for up to 30 minutes after creation. Your 24 hours of Premium begin only when you activate the pass after returning.")
        url = result["shortened_url"]
    elif status == "disabled":
        text, url = "Sponsor passes are currently unavailable.", None
    else:
        text, url = "Could not prepare a sponsor link. Please try again later.", None
    await _show(update, context, text, pass_keyboard(url))


async def redeem(update, context, token):
    from nodick.telegram import app
    if not await _private(update, context):
        return
    if not TOKEN.fullmatch(token):
        await _show(update, context, "This Premium pass link is invalid. Request a new pass.", pass_keyboard())
        return
    if await app._check_force_join(update, context, pending_action="pass:" + token):
        return
    result = await shortener.redeem_premium_pass(update.effective_user.id, token)
    status = result.get("status")
    if status == "activated":
        text = "✅ 1-Day Premium activated!\n\n" + _active_text(result)
    elif status == "active":
        text = _active_text(result)
    else:
        text = {
            "expired": "This Premium pass has expired. Request a new pass.",
            "invalid": "This Premium pass is invalid or already used. Request a new pass.",
            "disabled": "Sponsor passes are currently unavailable.",
        }.get(status, "Could not activate the pass. Please try again later.")
    await _show(update, context, text, pass_keyboard())


async def tutorial(update, context):
    if not await _private(update, context):
        return
    if update.callback_query:
        await update.callback_query.answer()
    value = tutorial_value()
    if not value:
        await _show(update, context, "A tutorial has not been set up yet. Follow the sponsor link steps and return using the final button.", pass_keyboard())
        return
    try:
        await context.bot.send_video(chat_id=update.effective_chat.id, video=value,
                                     caption="General walkthrough — individual provider pages may differ.", reply_markup=pass_keyboard())
    except Exception:
        await _show(update, context, "The tutorial is unavailable. Please contact an admin to update it.", pass_keyboard())


async def admin_settings(update, context):
    from nodick.telegram import app
    if not await _private(update, context):
        return
    if not app._is_admin(update):
        await update.callback_query.answer("Admin only.", show_alert=True)
        return
    await update.callback_query.answer()
    action = update.callback_query.data
    notice = ""
    if action == "sponsor_toggle":
        default = "1" if getattr(settings, "shortener_enabled", True) else "0"
        target = "0" if get_bot_setting("shortener_enabled", default) == "1" else "1"
        set_bot_setting("shortener_enabled", target)
        notice = "Updated.\n" if get_bot_setting("shortener_enabled", _UNSET) == target else "Could not save the setting.\n"
    elif action == "sponsor_clear":
        set_bot_setting("shortener_tutorial_video", "")
        notice = "Tutorial reset to the built-in walkthrough.\n" if get_bot_setting("shortener_tutorial_video", _UNSET) == "" else "Could not reset the tutorial.\n"
    elif action == "sponsor_upload":
        context.user_data["waiting_for_setting"] = "sponsor_tutorial"
        await _show(update, context, "Send a tutorial as a Telegram video (not as a file/document). Use /cancel to stop.")
        return
    if context.user_data.get("waiting_for_setting") == "sponsor_tutorial":
        context.user_data.pop("waiting_for_setting", None)
    enabled = get_bot_setting("shortener_enabled", "1" if getattr(settings, "shortener_enabled", True) else "0") == "1"
    text = (notice + "🎁 Sponsor Pass Settings\n\nPasses: " + ("ON" if enabled else "OFF") +
            "\nProvider ready: " + ("Yes" if available() else "No (disabled or not configured)") +
            "\nTutorial: " + ("Built-in walkthrough" if isinstance(tutorial_value(), Path) else "Custom video"))
    await _show(update, context, text, Markup([
        [Button("🔄 Toggle Passes", callback_data="sponsor_toggle")],
        [Button("🎬 Upload Tutorial", callback_data="sponsor_upload"), Button("↩️ Reset Tutorial", callback_data="sponsor_clear")],
        [Button("🔙 Settings", callback_data="settings")],
    ]))


async def tutorial_input(update, context):
    if context.user_data.get("waiting_for_setting") != "sponsor_tutorial":
        return
    # This handler owns only its private upload state; never consume other flows.
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    from nodick.telegram import app
    if not app._is_admin(update):
        context.user_data.pop("waiting_for_setting", None)
        await update.message.reply_text("Admin only.")
        raise ApplicationHandlerStop
    msg = update.message
    media = msg.video
    if not media:
        await msg.reply_text("Please send a Telegram video, not a file/document. Use /cancel to stop.")
        raise ApplicationHandlerStop
    value = media.file_id
    try:
        # A document file ID is not always usable by sendVideo. Validate the
        # actual delivery path before replacing the existing tutorial.
        await context.bot.send_video(chat_id=update.effective_chat.id, video=value,
                                     caption="Tutorial preview")
        set_bot_setting("shortener_tutorial_video", value)
        saved = get_bot_setting("shortener_tutorial_video", _UNSET)
        if saved != value:
            raise ValueError("Tutorial persistence failed")
    except Exception:
        await msg.reply_text("Could not save the tutorial. Please try again or /cancel.")
        raise ApplicationHandlerStop
    context.user_data.pop("waiting_for_setting", None)
    await msg.reply_text("✅ Tutorial saved.", reply_markup=Markup([[Button("🔙 Sponsor Settings", callback_data="sponsor_settings")]]))
    raise ApplicationHandlerStop
