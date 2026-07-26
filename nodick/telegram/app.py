"""NoDick Telegram bot — ALL handlers merged from both bots"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nodick.config import settings
from nodick.db import (
    category_count,
    connect,
    create_import_job,
    ensure_user_exists,
    find_sibling_parts,
    get_bot_setting,
    get_categories,
    get_favorites,
    get_video,
    get_video_metadata,
    get_videos_by_category,
    increment_view,
    init_db,
    latest_import_job,
    random_video as db_random,
    search_videos,
    set_bot_setting,
    total_views,
    upsert_video,
    video_count as db_video_count,
)
from nodick.services.importer import TelegramImporter
from nodick.services.message_importer import MessageIDImporter
from nodick.telegram.keyboards import (
    back,
    import_menu as import_keyboard,
    main_menu,
    part_nav_row,
    settings_keyboard,
    video_actions,
)
from nodick.utils import (
    clean_title_for_display,
    extract_category_from_title,
    format_duration,
    title_from_filename_or_caption,
)

log = logging.getLogger(__name__)

# ── Init stash integration (lazy) ──────────────────────────────────────────

_stash_available = False
try:
    from nodick.metadata.stash import (  # noqa: F401
        get_match_threshold,
        process_video_caption,
        set_match_threshold,
    )

    _stash_available = True
except ImportError as e:
    log.warning("Stash metadata module not available: %s", e)

    def process_video_caption(filename):  # type: ignore
        return None, "local"

    def set_match_threshold(value):  # type: ignore
        return 0.0

    def get_match_threshold():  # type: ignore
        return 0.0


# ── Constants ──────────────────────────────────────────────────────────────

WELCOME_MSG = """🖤 NoDick

Your fucking stash index. Cleaner than your browser history. 🥴

🎲 /random — surprise me
🔍 /search <keyword> — find shit
📊 /stats — collection flex
📥 /import <channel_id> — admin import

More:
📁 /categories — browse by studio
⭐ /favorites — your saved
⚙️ /settings — admin panel
🎭 /performer <name> — StashDB lookup
📏 /threshold <0-100> — match sensitivity"""


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == settings.admin_id)


async def _send_video_ref(
    bot, chat_id: int, file_ref: str, caption: str, reply_markup=None
):
    if file_ref.startswith("user_ref:"):
        _, channel_id, message_id = file_ref.split(":", 2)
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(channel_id),
            message_id=int(message_id),
            caption=caption,
            reply_markup=reply_markup,
        )
    elif file_ref.startswith("channel_ref:"):
        # Stored by the Telethon scanner: channel_ref:channel_id:message_id
        _, channel_id, message_id = file_ref.split(":", 2)
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(channel_id),
            message_id=int(message_id),
            caption=caption,
            reply_markup=reply_markup,
        )
    else:
        await bot.send_video(
            chat_id=chat_id,
            video=file_ref,
            caption=caption,
            reply_markup=reply_markup,
        )


async def _enrich_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    video_id: int,
):
    """Fetch video + optional stash enrichment, send to chat."""
    row = get_video(video_id)
    if not row:
        if update.callback_query:
            await update.callback_query.answer("Missing video", show_alert=True)
        return

    increment_view(video_id)
    filename = row["title"] or ""
    chat_id = update.effective_chat.id

    # Try stash enrichment
    caption_text = None
    source = "local"
    if _stash_available and filename:
        try:
            caption_text, source = process_video_caption(filename)
        except Exception as e:
            log.debug("Stash enrichment failed: %s", e)

    if not caption_text or source == "local":
        base = clean_title_for_display(filename)
        caption_text = (
            f"📁 *{base}*\n\n"
            f"⏱ {format_duration(row['duration'])} | 👁 {row['view_count'] + 1}"
        )

    meta = get_video_metadata(video_id)
    show_rename = bool(
        meta and meta.get("stashdb_confidence", 0) and meta["stashdb_confidence"] >= 0.9
    )
    markup = video_actions(
        video_id,
        show_rename=show_rename,
        feedback_enabled=_stash_available,
    )

    # Multi-part navigation — if this video has siblings, add prev/next buttons
    siblings = find_sibling_parts(video_id, filename)
    nav = part_nav_row(siblings, video_id)
    if nav:
        buttons = list(markup.inline_keyboard)
        buttons.insert(0, nav)
        markup = InlineKeyboardMarkup(buttons)

    await _send_video_ref(
        context.bot, chat_id, row["file_id"], caption_text, markup
    )


def _duration(seconds: Optional[int]) -> str:
    return format_duration(seconds)


# ── Command: /start ────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        ensure_user_exists(user.id)
    markup = main_menu(user.id if user else None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            WELCOME_MSG, reply_markup=markup
        )
    else:
        await update.message.reply_text(WELCOME_MSG, reply_markup=markup)


# ── Command: /random ───────────────────────────────────────────────────────


async def random_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Opening the vault...")
    row = db_random()
    if not row:
        text = "🥺 NoDick is empty. Send a video or use /import."
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=main_menu(update.effective_user.id)
            )
        else:
            await update.message.reply_text(
                text, reply_markup=main_menu(update.effective_user.id)
            )
        return

    await _enrich_and_send(update, context, row["id"])


# ── Command: /search ───────────────────────────────────────────────────────


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <keyword>")
        return
    query = " ".join(context.args)
    await _show_search(update, context, query=query, page=0)


async def search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data["waiting_for_search"] = True
    await update.callback_query.edit_message_text(
        "🔍 Send me a search keyword:", reply_markup=back()
    )


async def _show_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str,
    page: int = 0,
):
    per_page = 10
    rows, total = search_videos(query, page=page, per_page=per_page)

    if not rows:
        text, markup = "🥺 No matches.", back()
    else:
        buttons = [
            [
                InlineKeyboardButton(
                    f"{clean_title_for_display(r['title'])[:35]} ({_duration(r['duration'])})",
                    callback_data=f"play_{r['id']}",
                )
            ]
            for r in rows
        ]
        # Pagination
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "⬅️", callback_data=f"searchpage_{query}|{page - 1}"
                )
            )
        if (page + 1) * per_page < total:
            nav.append(
                InlineKeyboardButton(
                    "➡️", callback_data=f"searchpage_{query}|{page + 1}"
                )
            )
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
        text = f"🔍 {query} — {len(rows)} of {total}"
        markup = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def search_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, payload = q.data.split("_", 1)
    query, page = payload.rsplit("|", 1)
    await _show_search(update, context, query=query, page=int(page))


# ── Command: /stats ────────────────────────────────────────────────────────


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    total = db_video_count()
    views = total_views()
    cats = category_count()
    text = (
        f"📊 NoDick Stats\n\n"
        f"📹 Videos: {total:,}\n"
        f"👁 Views: {views:,}\n"
        f"📁 Categories: {cats:,}"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back())
    else:
        await update.message.reply_text(
            text, reply_markup=main_menu(update.effective_user.id)
        )


# ── Command: /categories ──────────────────────────────────────────────────


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    cats = get_categories()
    if not cats:
        text = "📁 No categories yet."
        markup = back()
    else:
        buttons = [
            InlineKeyboardButton(
                f"{cat['category']} ({cat['count']})",
                callback_data=f"cat_{cat['category']}",
            )
            for cat in cats
        ]
        # 2 per row
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
        markup = InlineKeyboardMarkup(rows)
        text = f"📁 *Categories* — {len(cats)} total"

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("cat_"):
        category = data.split("_", 1)[1]
        page = 0
    elif data.startswith("catpage_"):
        _, payload = data.split("_", 1)
        category, page = payload.rsplit("|", 1)
        page = int(page)
    else:
        return

    per_page = 10
    rows, total = get_videos_by_category(category, page=page, per_page=per_page)
    if not rows:
        await q.edit_message_text("🥺 No videos.", reply_markup=back())
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"{clean_title_for_display(r['title'])[:35]} ({_duration(r['duration'])})",
                callback_data=f"play_{r['id']}",
            )
        ]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️", callback_data=f"catpage_{category}|{page - 1}"
            )
        )
    if (page + 1) * per_page < total:
        nav.append(
            InlineKeyboardButton(
                "➡️", callback_data=f"catpage_{category}|{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Categories", callback_data="categories")])
    buttons.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])

    await q.edit_message_text(
        f"📁 {category} — {len(rows)} of {total}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Command: /favorites ────────────────────────────────────────────────────


async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = update.effective_user.id
    await q.answer()

    page = 0
    if q.data.startswith("favpage_"):
        page = int(q.data.split("_", 1)[1])

    per_page = 10
    rows, total = get_favorites(user_id, page=page, per_page=per_page)
    if not rows:
        await q.edit_message_text(
            "⭐ No favorites yet. Tap 💦 on a video to save it.",
            reply_markup=main_menu(user_id),
        )
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"{clean_title_for_display(r['title'])[:35]} ({_duration(r['duration'])})",
                callback_data=f"play_{r['id']}",
            )
        ]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️", callback_data=f"favpage_{page - 1}")
        )
    if (page + 1) * per_page < total:
        nav.append(
            InlineKeyboardButton("➡️", callback_data=f"favpage_{page + 1}")
        )
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])

    await q.edit_message_text(
        f"⭐ Favorites — {len(rows)} of {total}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def add_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⭐ Toggled!")
    video_id = int(q.data.split("_", 1)[1])
    from nodick.db import toggle_favorite

    toggle_favorite(update.effective_user.id, video_id)


# ── Play (callback from inline buttons) ────────────────────────────────────


async def play_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Loading...")
    video_id = int(q.data.split("_", 1)[1])
    await _enrich_and_send(update, context, video_id)


# ── Command: /threshold ────────────────────────────────────────────────────


async def threshold_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set StashDB match threshold."""
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return

    if not context.args:
        current = get_match_threshold()
        await update.message.reply_text(
            f"📊 Current threshold: {current:.0%}\n\n"
            f"Usage: /threshold <0-100>\n"
            f"Example: /threshold 70\n\n"
            f"At 80, only matches ≥80% confidence are used.\n"
            f"At 0 (default), all results are shown."
        )
        return

    try:
        value = int(context.args[0])
        if value < 0 or value > 100:
            await update.message.reply_text("❌ Threshold must be 0-100.")
            return
        new_threshold = set_match_threshold(value / 100.0)
        await update.message.reply_text(f"✅ Threshold set to {new_threshold:.0%}")
    except ValueError:
        await update.message.reply_text("❌ Provide a number 0-100.")


# ── Command: /performer ────────────────────────────────────────────────────


async def performer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search StashDB/FansDB for performers."""
    if not _stash_available:
        await update.message.reply_text("❌ Performer search unavailable (stash module missing).")
        return

    if not context.args:
        await update.message.reply_text("Usage: /performer <name>\nExample: /performer 'Riley Reid'")
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"🔍 Searching for '{query}'...")

    try:
        from nodick.metadata.performer_db import search_performers_fast, format_performer_info

        performers = search_performers_fast(query, limit=5)
        if not performers:
            await update.message.reply_text("🥺 No performers found.")
            return

        lines = [f"🎭 *Performer Search: '{query}'*\n"]
        for p in performers:
            lines.append(format_performer_info(p))
            lines.append("")

        await update.message.reply_text(
            "\n".join(lines), parse_mode="Markdown"
        )
    except Exception as e:
        log.error("Performer search error: %s", e)
        await update.message.reply_text(f"❌ Search failed: {e}")


# ── Command: /settings ────────────────────────────────────────────────────


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "⚙️ *Settings*", reply_markup=settings_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚙️ Settings", reply_markup=settings_keyboard())


async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        await q.edit_message_text("❌ Admin only.", reply_markup=back())
        return

    setting = q.data.replace("toggle_", "")
    if setting == "action_buttons":
        current = get_bot_setting("action_buttons_enabled", "1")
        set_bot_setting("action_buttons_enabled", "0" if current == "1" else "1")
        await q.edit_message_text(
            "⚙️ Settings toggled.", reply_markup=settings_keyboard()
        )


# ── Command: /import ──────────────────────────────────────────────────────


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    channel_id = (
        context.args[0] if context.args else settings.default_import_channel
    )
    if not channel_id:
        await update.message.reply_text("Usage: /import <channel_id>")
        return
    await _start_import(update, context, channel_id)


async def import_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(update):
        await q.answer("Admin only", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(
        "🛰 NoDick Importer\n\n"
        "Use /import <channel_id> or import the default configured channel.\n\n"
        "Requires a logged-in Telethon user session (run `python -m nodick session-login` first).",
        reply_markup=import_keyboard(),
    )


async def import_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(update):
        await q.answer("Admin only", show_alert=True)
        return
    await q.answer("Starting import...")
    await _start_import(update, context, settings.default_import_channel)


async def _start_import(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str):
    user_id = update.effective_user.id
    job_id = create_import_job(channel_id, user_id)
    message = update.message or update.callback_query.message
    status_msg = await message.reply_text(
        f"🛰 Import job #{job_id} started for {channel_id}"
    )

    async def run_job():
        importer = TelegramImporter()
        try:
            async def progress(stats):
                if (
                    stats.get("videos_found", 0) % 100 == 0
                    or stats.get("status") in {"done", "failed"}
                ):
                    try:
                        await status_msg.edit_text(
                            f"🛰 Import #{job_id}: {stats['status']}\n"
                            f"Checked: {stats['total_checked']}\n"
                            f"Videos: {stats['videos_found']}\n"
                            f"Saved: {stats['saved']}\n"
                            f"Skipped: {stats['skipped']}"
                        )
                    except Exception:
                        pass

            await importer.import_channel(channel_id, job_id=job_id, progress=progress)
        except Exception as exc:
            log.exception("Import failed")
            await status_msg.edit_text(f"❌ Import #{job_id} failed:\n{exc}")
        finally:
            await importer.disconnect()

    context.application.create_task(run_job())


async def import_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    row = latest_import_job()
    text = (
        "No import jobs yet."
        if not row
        else (
            f"🛰 Latest import #{row['id']}\n"
            f"Status: {row['status']}\n"
            f"Channel: {row['channel_id']}\n"
            f"Checked: {row['total_checked']}\n"
            f"Videos: {row['videos_found']}\n"
            f"Saved: {row['saved']}\n"
            f"Skipped: {row['skipped']}\n"
            f"Error: {row['error'] or '-'}"
        )
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=import_keyboard())
    else:
        await update.message.reply_text(text)


# ── Command: /import_scan ──────────────────────────────────────────────────
# Bot API sequential message ID import — no Telethon needed.
# The bot must be an admin in the source channel.


async def import_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: start sequential message ID import.
    Usage: /import_scan <channel_id> [start_message_id]
    """
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /import_scan <channel_id> [start_message_id]\n\n"
            "Scans message IDs from start_id downward, copies videos into NoDick.\n"
            "Bot must be an admin in the channel.\n\n"
            "Alternatively, forward the latest video from your DB channel\n"
            "to this bot and it'll auto-detect the source."
        )
        return

    channel_arg = context.args[0]
    try:
        channel_id = int(channel_arg)
    except ValueError:
        await update.message.reply_text("❌ channel_id must be an integer (e.g., -1001234567890)")
        return

    start_id = int(context.args[1]) if len(context.args) > 1 else None

    if start_id:
        await _start_message_import(update, context, channel_id, start_id)
    else:
        await update.message.reply_text(
            "❌ Provide a start message ID.\n"
            "Forward the latest video from your channel to the bot and I'll detect it automatically."
        )


async def _start_message_import(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    channel_id: int,
    start_id: int,
):
    """Fire up the MessageIDImporter."""
    user_id = update.effective_user.id
    message = update.message or update.callback_query.message
    status_msg = await message.reply_text(
        f"🛰 Scanning {channel_id} from message {start_id} down..."
    )

    importer = MessageIDImporter(context.bot, staging_chat_id=user_id)

    try:
        async def progress(stats):
            if (
                stats.get("total_checked", 0) % 50 == 0
                or stats.get("status") in {"done", "failed"}
            ):
                try:
                    await status_msg.edit_text(
                        f"🛰 Scan: {stats['status']}\n"
                        f"Checked: {stats['total_checked']:,}\n"
                        f"Videos: {stats['videos_found']}\n"
                        f"Saved: {stats['saved']}\n"
                        f"Skipped: {stats['skipped']}"
                    )
                except Exception:
                    pass

        stats = await importer.import_channel(
            channel_id=channel_id,
            start_message_id=start_id,
            progress=progress,
        )

        await status_msg.edit_text(
            f"✅ Scan complete!\n"
            f"Checked: {stats['total_checked']:,}\n"
            f"Videos found: {stats['videos_found']}\n"
            f"Saved: {stats['saved']}\n"
            f"Skipped: {stats['skipped']}"
        )
    except Exception as e:
        log.exception("Scan failed")
        await status_msg.edit_text(f"❌ Scan failed: {e}")


# ── Forwarded message handler ──────────────────────────────────────────────
# When admin forwards the latest video from a DB channel, detect source
# and offer to start import.


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect forwarded messages from channels and offer to import."""
    if not _is_admin(update):
        return

    msg = update.message
    if not msg:
        return

    # PTB v21: forward info is in forward_origin, forward_from_chat is gone
    origin = msg.forward_origin
    if not origin:
        return

    from telegram._messageorigin import MessageOriginChannel

    if not isinstance(origin, MessageOriginChannel):
        return

    channel_id = origin.chat.id
    message_id = origin.message_id

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📥 Import from here",
            callback_data=f"scanimport_{channel_id}_{message_id}",
        )],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu")],
    ])

    await msg.reply_text(
        f"📡 Detected channel: `{channel_id}`\n"
        f"🔖 Latest message ID: `{message_id}`\n\n"
        f"Start sequential scan from here? I'll walk backwards through\n"
        f"every message ID and index any videos I find.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def scanimport_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback from the forward detection keyboard."""
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        await q.edit_message_text("❌ Admin only.")
        return

    _, channel_id_str, message_id_str = q.data.split("_", 2)
    channel_id = int(channel_id_str)
    message_id = int(message_id_str)

    await q.edit_message_text(
        f"🛰 Starting scan of {channel_id} from message {message_id}..."
    )

    importer = MessageIDImporter(context.bot, staging_chat_id=update.effective_user.id)

    try:
        async def progress(stats):
            if (
                stats.get("total_checked", 0) % 50 == 0
                or stats.get("status") in {"done", "failed"}
            ):
                try:
                    await q.edit_message_text(
                        f"🛰 Scan: {stats['status']}\n"
                        f"Checked: {stats['total_checked']:,}\n"
                        f"Videos: {stats['videos_found']}\n"
                        f"Saved: {stats['saved']}\n"
                        f"Skipped: {stats['skipped']}"
                    )
                except Exception:
                    pass

        stats = await importer.import_channel(
            channel_id=channel_id,
            start_message_id=message_id,
            progress=progress,
        )

        await q.edit_message_text(
            f"✅ Scan complete!\n"
            f"Checked: {stats['total_checked']:,}\n"
            f"Videos found: {stats['videos_found']}\n"
            f"Saved: {stats['saved']}\n"
            f"Skipped: {stats['skipped']}"
        )
    except Exception as e:
        log.exception("Scan failed")
        await q.edit_message_text(f"❌ Scan failed: {e}")


# ── Video upload handler ───────────────────────────────────────────────────


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    video = msg.video or (
        msg.document
        if msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("video/")
        else None
    )
    if not video:
        return

    filename = getattr(video, "file_name", None)
    title = title_from_filename_or_caption(
        filename, msg.caption, f"Video_{msg.message_id}"
    )
    saved = upsert_video(
        file_id=video.file_id,
        title=title,
        duration=getattr(video, "duration", 0) or 0,
        category=extract_category_from_title(title),
        file_unique_id=getattr(video, "file_unique_id", None),
        file_size=getattr(video, "file_size", None),
    )
    await msg.reply_text(
        ("✅ Indexed" if saved else "⚠️ Already indexed") + f": {title[:80]}",
        reply_markup=main_menu(update.effective_user.id),
    )


# ── Text message router ────────────────────────────────────────────────────


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Check if waiting for search query
    if context.user_data.pop("waiting_for_search", False):
        await _show_search(update, context, query=text, page=0)
        return

    # Check if waiting for correction text
    if context.user_data.pop("waiting_for_correction", None):
        await _handle_correction_text(update, context, text)
        return

    # Otherwise, treat as search
    await _show_search(update, context, query=text, page=0)


# ── Correction system ──────────────────────────────────────────────────────


async def handle_correct_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    video_id = int(q.data.split("_", 1)[1])
    context.user_data["waiting_for_correction"] = video_id
    await q.edit_message_text(
        "✏️ Send me the correct title for this video.\n"
        "Format: `Performer — Title`\n"
        "Or send /cancel to cancel.",
        reply_markup=back(),
    )


async def _handle_correction_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
):
    video_id = context.user_data["waiting_for_correction"]
    # Parse correction
    performer, title = None, text
    if " — " in text:
        parts = text.split(" — ", 1)
        performer = parts[0].strip()
        title = parts[1].strip()
    elif " - " in text:
        parts = text.split(" - ", 1)
        performer = parts[0].strip()
        title = parts[1].strip()

    from nodick.db import upsert_video_metadata

    upsert_video_metadata(
        video_id,
        corrected_performer=performer,
        corrected_title=title,
        corrected_by_user=update.effective_user.id,
    )

    # Update the main title too
    if title:
        from nodick.db import _using_pg as _db_pg
        ph = "%s" if _db_pg else "?"
        with connect() as conn:
            conn.execute(f"UPDATE videos SET title = {ph} WHERE id = {ph}", (text, video_id))

    await update.message.reply_text("✅ Corrected! 💕", reply_markup=main_menu(update.effective_user.id))


# ── Rename callback ────────────────────────────────────────────────────────


async def handle_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Rename will be available via auto_rename module 📝")
    video_id = int(q.data.split("_", 1)[1])
    meta = get_video_metadata(video_id)
    if meta and meta.get("stashdb_title"):
        suggested = f"{meta['stashdb_performer'] or ''} — {meta['stashdb_title']}".strip(" —")
        await q.edit_message_text(
            f"📝 Suggested rename: `{suggested}`\n\n"
            f"Use /correct to apply a custom name.",
            reply_markup=back(),
        )


# ── Error handler ──────────────────────────────────────────────────────────


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Update %s caused error %s", update, context.error)


# ── No-op callback (for non-interactive info buttons) ──────────────────────


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently acknowledge non-interactive buttons (e.g., part counter)."""
    await update.callback_query.answer()


# ── Application builder ────────────────────────────────────────────────────


def build_application() -> Application:
    init_db()
    app = Application.builder().token(settings.bot_token).build()

    # ── Commands ──
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_video))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("favorites", show_favorites))
    app.add_handler(CommandHandler("performer", performer_command))
    app.add_handler(CommandHandler("threshold", threshold_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("import", import_command))
    app.add_handler(CommandHandler("import_status", import_status))
    app.add_handler(CommandHandler("import_scan", import_scan_command))

    # ── Callbacks ──
    app.add_handler(CallbackQueryHandler(start, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(random_video, pattern="^random$"))
    app.add_handler(CallbackQueryHandler(search_menu, pattern="^search_menu$"))
    app.add_handler(CallbackQueryHandler(categories, pattern="^categories$"))
    app.add_handler(CallbackQueryHandler(search_page_handler, pattern="^searchpage_"))
    app.add_handler(CallbackQueryHandler(play_video, pattern="^play_"))
    app.add_handler(CallbackQueryHandler(show_category, pattern="^catpage_"))
    app.add_handler(CallbackQueryHandler(show_category, pattern="^cat_"))
    app.add_handler(CallbackQueryHandler(show_favorites, pattern="^favpage_"))
    app.add_handler(CallbackQueryHandler(add_favorite, pattern="^fav_"))
    app.add_handler(CallbackQueryHandler(show_favorites, pattern="^favorites$"))
    app.add_handler(CallbackQueryHandler(import_menu_handler, pattern="^import_menu$"))
    app.add_handler(CallbackQueryHandler(import_default, pattern="^import_default$"))
    app.add_handler(CallbackQueryHandler(import_status, pattern="^import_status$"))
    app.add_handler(CallbackQueryHandler(stats, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(settings_command, pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(toggle_setting, pattern="^toggle_"))
    app.add_handler(CallbackQueryHandler(handle_rename_callback, pattern="^rename_"))
    app.add_handler(CallbackQueryHandler(handle_correct_callback, pattern="^correct_"))
    app.add_handler(CallbackQueryHandler(scanimport_callback, pattern="^scanimport_"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))

    # ── Message handlers ──
    # Forwarded-from-channel detection must come BEFORE video handler
    # so it can offer batch import instead of single index
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forward))
    # Video handler needs to skip forwarded-from-channel messages
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.FORWARDED, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # ── Error handler ──
    app.add_error_handler(error_handler)

    return app


def run() -> None:
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN missing. Copy .env.example to .env and fill it.")

    import sys as _sys
    import os as _os
    import threading as _threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    if _sys.version_info >= (3, 10):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    # Start a minimal health-check web server so Render knows we're alive
    # and doesn't spin down the free tier service
    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"NoDick is alive")
            self.wfile.flush()

        def log_message(self, format, *args):
            pass  # suppress request logs

    _port = int(_os.getenv("PORT", "8080"))
    _server = HTTPServer(("0.0.0.0", _port), _HealthHandler)
    _t = _threading.Thread(target=_server.serve_forever, daemon=True)
    _t.start()
    log.info("Health server on port %d", _port)

    print("🖤 NoDick starting...")
    print(f"   Bot: @{settings.bot_token.split(':')[0]}")
    print(f"   Admin: {settings.admin_id}")
    print(f"   DB: {settings.db_path}")
    print(f"   Database URL: {'✅ Set (PostgreSQL)' if settings.database_url else '❌ Not set (using SQLite)'}")
    print(f"   StashDB: {'✅' if settings.stash_configured else '❌ no API key'}")
    print(f"   FansDB: {'✅' if settings.fansdb_configured else '❌ no API key'}")
    print(f"   Threshold: {get_match_threshold():.0%}")
    print(f"   Health: http://0.0.0.0:{_port}/")
    print("   Listening...")

    build_application().run_polling()
