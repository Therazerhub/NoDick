"""NoDick Telegram bot — ALL handlers merged from both bots"""

from __future__ import annotations

import asyncio
import base64
import logging
import json
from datetime import datetime, timedelta
import re
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter
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
    get_similar_videos,
    get_user_size_limit,
    get_video,
    get_video_metadata,
    get_videos_by_category,
    get_videos_by_performer,
    increment_view,
    init_db,
    latest_import_job,
    performer_video_count,
    random_video as db_random,
    save_enrichment,
    search_videos,
    set_bot_setting,
    set_user_size_limit,
    total_views,
    update_video_size,
    upsert_video,
    video_count as db_video_count,
    is_user_premium,
    get_user_quota,
    increment_quota,
    grant_premium,
    revoke_premium,
    list_premium_users,
    record_referral,
    get_referral_count,
    get_all_user_ids,
    user_exists,
    get_user_count,
)
from nodick.services.importer import TelegramImporter
from nodick.services.message_importer import (
    MessageIDImporter,
    _get_telethon_client as _get_telethon_bot,
)
from nodick.telegram.keyboards import (
    back,
    refer_keyboard,
    import_menu as import_keyboard,
    scan_running_keyboard,
    main_menu,
    part_nav_row,
    quality_keyboard,
    settings_keyboard,
    video_actions,
    account_keyboard,
    force_join_keyboard,
    ad_button_keyboard,
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
        process_video_caption_with_metadata,
        set_match_threshold,
    )

    _stash_available = True
except ImportError as e:
    log.warning("Stash metadata module not available: %s", e)

    def process_video_caption(filename):  # type: ignore
        return None, "local"

    def process_video_caption_with_metadata(filename):  # type: ignore
        return None, "local", {}

    def set_match_threshold(value):  # type: ignore
        return 0.0

    def get_match_threshold():  # type: ignore
        return 0.0


# ── Constants ──────────────────────────────────────────────────────────────

WELCOME_MSG = """𝕿𝖍𝖊 𝖁𝖆𝖚𝖑𝖙 𝖎𝖘 𝕺𝖕𝖊𝖓. 🖤

━━━━━━━━━━━━━━━━━━━━

*Your personal, zero-clutter stash of premium organized filth.* 💦

⏳ _Warning: Videos self-destruct in 30 minutes. Save what makes you ache before the ghost protocol wipes them._ 👻

**Dive in. I know you want to.** 😈

━━━━━━━━━━━━━━━━━━━━

⚡ _Architected by_ [The Razer](tg://user?id=6001922744) 🔥"""


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_admin(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return False
    if user_id == settings.admin_id:
        return True
    from nodick.db import get_bot_setting
    extra = get_bot_setting("extra_admins", "")
    if extra:
        try:
            return user_id in [int(x) for x in extra.split()]
        except ValueError:
            pass
    return False


# ── Force Join check ──────────────────────────────────────────────────────

def _get_force_join_channels() -> list[dict]:
    raw = get_bot_setting("force_join_channels", "[]")
    try:
        return json.loads(raw) or []
    except (json.JSONDecodeError, TypeError):
        return []

async def _check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_admin(update):
        return False
    channels = _get_force_join_channels()
    if not channels:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return False
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(ch["id"], user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    if not not_joined:
        return False
    text = (
        "🔒 *Hold up, curious one…*\n\n"
        "You need to join our channels first before I let you in. 😈\n\n"
        "Join them all, then tap *✅ I have Joined*."
    )
    markup = force_join_keyboard(not_joined)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    return True

async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        await q.answer("❌ Can't verify", show_alert=True)
        return
    channels = _get_force_join_channels()
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(ch["id"], user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    if not_joined:
        names = ", ".join(ch["name"] for ch in not_joined)
        await q.answer(f"❌ Still not joined: {names}", show_alert=True)
        return
    await q.answer("✅ Verified! Welcome in… 😏")
    markup = main_menu(user_id)
    welcome_gif = get_bot_setting("welcome_gif", "")
    
    if welcome_gif:
        try:
            # We must delete the old text lock screen and send the new GIF
            await q.message.delete()
            await context.bot.send_animation(
                chat_id=q.message.chat_id,
                animation=welcome_gif,
                caption=WELCOME_MSG,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception as e:
            pass # fallback to edit text
            
    await q.edit_message_text(WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

# ── In-Bot Ad system ──────────────────────────────────────────────────────

def _get_ad_config() -> dict | None:
    raw = get_bot_setting("ad_config", "")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
        if cfg and cfg.get("text") and cfg.get("button_text") and cfg.get("button_url"):
            return cfg
    except (json.JSONDecodeError, TypeError):
        pass
    return None

def _get_ad_frequency() -> int:
    raw = get_bot_setting("ad_frequency", "5")
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        return 5

async def _maybe_send_ad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    count = context.user_data.get("video_count", 0) + 1
    context.user_data["video_count"] = count
    freq = _get_ad_frequency()
    if count % freq != 0:
        return
    ad = _get_ad_config()
    if not ad:
        return
    chat_id = update.effective_chat.id
    try:
        markup = ad_button_keyboard(ad["button_text"], ad["button_url"])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📣 *Sponsored*\n\n{ad['text']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
    except Exception as e:
        log.debug("Failed to send ad: %s", e)

# ── Auto Delete ──────────────────────────────────────────────────────────

async def _auto_delete_message(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass

async def _send_video_ref(
    bot, chat_id: int, file_ref: str, caption: str, reply_markup=None
):
    """Send/copy a video ref with RetryAfter (flood control) handling.

    Telegram rate-limits copyMessage from channels ("Flood control exceeded,
    retry in N seconds"). Instead of dying instantly (which surfaces as
    "⚠️ Something broke" to the user), wait out the delay and retry.
    """
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            if file_ref.startswith("user_ref:"):
                _, channel_id, message_id = file_ref.split(":", 2)
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=int(channel_id),
                    message_id=int(message_id),
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
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
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            else:
                await bot.send_video(
                    chat_id=chat_id,
                    video=file_ref,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            return
        except RetryAfter as e:
            if attempt >= max_retries:
                raise
            delay = min(float(getattr(e, "retry_after", 5) or 5), 30)
            log.warning(
                "Flood control on send (%s), waiting %.0fs (attempt %d/%d)",
                file_ref[:30], delay, attempt + 1, max_retries,
            )
            await asyncio.sleep(delay)
        except Exception as e:
            log.error("Failed to send video ref %s: %s", file_ref[:30], e)
            # Try to notify user if we have a callback query context
            raise

    # For auto-delete, we need to return the message or message ID
    if file_ref.startswith("user_ref:") or file_ref.startswith("channel_ref:"):
        _, channel_id, message_id = file_ref.split(":", 2)
        try:
            msg_id = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(channel_id),
                message_id=int(message_id),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
            return msg_id
        except Exception:
            return None
    else:
        try:
            msg = await bot.send_video(
                chat_id=chat_id,
                video=file_ref,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
            return msg
        except Exception:
            return None


def _caption_from_cache(
    meta, row_tags: list[str], filename: str, row
) -> Optional[str]:
    """Build the enriched-style caption from cached metadata — instant, no network.

    Respects user corrections (corrected_* win over stashdb_*). Returns None
    when the cache has nothing usable (then live enrichment / fallback runs).
    """
    md = dict(meta)
    title = md.get("corrected_title") or md.get("stashdb_title")
    if not title:
        return None
    performers = (md.get("corrected_performer") or md.get("stashdb_performer") or "").strip(",")
    studio = md.get("corrected_studio") or md.get("stashdb_studio") or ""
    head = f"🌐 *{performers} — {title}*" if performers else f"🌐 *{title}*"
    tag_line = " ".join(f"`#{t}`" for t in row_tags[:5])
    body = f"━━━━━━━━━━━━━━\n{tag_line}"
    if studio:
        return f"{head}\n`{studio}`\n{body}"
    return f"{head}\n{body}"


async def _enrich_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    video_id: int,
    allow_redirect: bool = True,
):
    """Fetch video + optional stash enrichment, send to chat.

    If ``allow_redirect`` is True and the video is part of a multi-part set
    but not part 1, automatically redirect to part 1.
    """
    row = get_video(video_id)
    if not row:
        if update.callback_query:
            await update.callback_query.answer("Missing video", show_alert=True)
        return

    filename = row["title"] or ""

    # Auto-redirect to part 1 if this is a multi-part video (only for auto-picks)
    if allow_redirect:
        siblings = find_sibling_parts(video_id, filename)
        if siblings:
            part1 = next((s for s in siblings if s["part"] == 1), None)
            if part1 and part1["id"] != video_id:
                log.info("Redirecting to part 1 (was part %s)", next((s["part"] for s in siblings if s["id"] == video_id), "?"))
                video_id = part1["id"]
                row = get_video(video_id)
                if not row:
                    if update.callback_query:
                        await update.callback_query.answer("Missing video", show_alert=True)
                    return
                filename = row["title"] or ""

    increment_view(video_id)
    chat_id = update.effective_chat.id

    meta = get_video_metadata(video_id)
    row_dict = dict(row)
    row_tags = [t for t in (row_dict.get("tags") or "").split(",") if t]
    cast = [p for p in (dict(meta).get("stashdb_performer") or "").split(",") if p] if meta else []

    # Caption strategy: cache-first (instant, zero StashDB calls), then live
    # enrichment (uncached videos), then the clean local fallback.
    caption_text = None
    source = "local"
    cache_used = False

    if meta:
        cached = _caption_from_cache(meta, row_tags, filename, row)
        if cached:
            caption_text = cached
            source = "cache"
            cache_used = True

    if not cache_used and _stash_available and filename:
        try:
            loop = asyncio.get_event_loop()
            caption_text, source, enrich_meta = await asyncio.wait_for(
                loop.run_in_executor(
                    None, process_video_caption_with_metadata, filename
                ),
                timeout=8.0,
            )
            # Persist the enrichment so "More Like This" / Cast / tag search
            # work from the cache instead of re-querying StashDB every view.
            if source == "stashdb" and save_enrichment(video_id, source, enrich_meta):
                # Freshly cached → refresh buttons NOW so 🎯/👤 appear on the
                # first play, not the second one. Re-read BOTH the metadata and
                # the tags from the DB (the in-memory row predates the cache write).
                meta = get_video_metadata(video_id)
                fresh = get_video(video_id)
                row_dict = dict(fresh) if fresh else {}
                row_tags = [t for t in (row_dict.get("tags") or "").split(",") if t]
                cast = [p for p in (dict(meta).get("stashdb_performer") or "").split(",") if p] if meta else []
        except asyncio.TimeoutError:
            log.warning("Stash enrichment timed out for: %s", filename[:50])
        except Exception as e:
            log.debug("Stash enrichment failed: %s", e)

    if not cache_used and (not caption_text or source == "local"):
        base = clean_title_for_display(filename)
        caption_text = (
            f"📁 *{base}*\n\n"
            f"⏱ {format_duration(row['duration'])} | 👁 {row['view_count'] + 1}"
        )

    show_rename = bool(
        meta and meta.get("stashdb_confidence", 0) and meta["stashdb_confidence"] >= 0.9
    )
    markup = video_actions(
        video_id,
        show_rename=show_rename,
        feedback_enabled=_stash_available,
        show_similar=bool(row_tags or cast),
        performers=cast[:4],
        user_id=update.effective_user.id if update.effective_user else None,
        is_admin=_is_admin(update),
    )

    # Multi-part navigation — if this video has siblings, add prev/next buttons
    siblings = find_sibling_parts(video_id, filename)
    nav = part_nav_row(siblings, video_id)
    if nav:
        buttons = list(markup.inline_keyboard)
        buttons.insert(0, nav)
        markup = InlineKeyboardMarkup(buttons)

    log.info("_enrich_and_send: calling _send_video_ref for video_id=%s", video_id)
    sent_msg = await _send_video_ref(
        context.bot, chat_id, row["file_id"], caption_text, markup
    )
    log.info("_enrich_and_send: _send_video_ref returned successfully")
    
    await _maybe_send_ad(update, context)
    
    if get_bot_setting("auto_delete_enabled", "1") == "1" and not _is_admin(update):
        delete_mins = int(get_bot_setting("auto_delete_minutes", "30"))
        if sent_msg:
            msg_id = getattr(sent_msg, 'message_id', sent_msg)
            if hasattr(msg_id, "message_id"):
                msg_id = msg_id.message_id
            if isinstance(msg_id, int):
                context.job_queue.run_once(
                    _auto_delete_message,
                    when=delete_mins * 60,
                    data={"chat_id": chat_id, "message_id": msg_id},
                    name=f"autodel_{chat_id}_{msg_id}",
                )

    # Lazy size backfill — fire-and-forget when we don't know the size yet
    if not row.get("file_size"):
        asyncio.create_task(_lazy_fill_size(video_id, row["file_id"]))


def _duration(seconds: Optional[int]) -> str:
    return format_duration(seconds)

async def _replace_with_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup=None):
    """Safely replace the current message with text, deleting it if it is media (GIF/Video)."""
    q = update.callback_query
    msg = q.message
    if msg.video or msg.document or msg.animation or msg.photo:
        try:
            await msg.delete()
        except Exception:
            pass
        return await context.bot.send_message(
            chat_id=msg.chat_id, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )
    else:
        return await q.edit_message_text(
            text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )


# ── Command: /start ────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        is_new = not user_exists(user.id)
        ensure_user_exists(user.id)
        if is_new:
            if context.args and context.args[0].startswith("ref_"):
                try:
                    referrer_id = int(context.args[0].replace("ref_", ""))
                    bonus = int(get_bot_setting("referral_bonus", "10"))
                    if record_referral(referrer_id, user.id, bonus):
                        refs = get_referral_count(referrer_id)
                        if refs > 0 and refs % 10 == 0:
                            grant_premium(referrer_id, 30)
                            await _log_event(context, f"✅ New referral: `{user.id}` joined via `{referrer_id}`. 🎁 Referrer hit {refs} and got 30 days Premium!")
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id, 
                                    text=f"🎉 *Congratulations!*\n\nYou just hit {refs} referrals! As a reward, you've unlocked *1 Month of Premium*! 👑\n\nEnjoy the unrestricted access.",
                                    parse_mode=ParseMode.MARKDOWN
                                )
                            except Exception:
                                pass
                        else:
                            await _log_event(context, f"✅ New referral: `{user.id}` joined via `{referrer_id}`")
                except ValueError:
                    pass
            username = f"@{user.username}" if user.username else "No username"
            full_name = filter(None, [user.first_name, user.last_name])
            name_str = " ".join(full_name) or "Unknown"
            await _log_event(context, f"👤 *New User*\nID: `{user.id}`\nUsername: {username}\nName: {name_str}")
            
    if context.args and context.args[0].startswith("vid_"):
        if await _check_force_join(update, context):
            return
        try:
            vid_id = int(context.args[0].replace("vid_", ""))
            # Quota Check logic
            if not _is_admin(update) and not is_user_premium(user.id):
                from nodick.db import increment_quota, get_user_quota
                if not increment_quota(user.id):
                    quota = get_user_quota(user.id)
                    text = (
                        "🚫 *You've used all your free watches!*\n\n"
                        f"📊 Used: {quota['used']}/{quota['limit']}\n\n"
                        "💡 *Get more:*\n"
                        "🔗 Refer friends to earn +10 each\n"
                        "👑 Or grab Premium for unlimited access\n"
                    )
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔗 Refer & Earn", callback_data="refer")],
                        [InlineKeyboardButton("👑 Get Premium", callback_data="get_premium")],
                        [InlineKeyboardButton("🔙 Menu", callback_data="menu")],
                    ])
                    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
                    return
            
            await _enrich_and_send(update, context, vid_id, allow_redirect=True)
            return
        except Exception as e:
            log.error(f"Error handling vid deep link: {e}")

    if await _check_force_join(update, context):
        return
    markup = main_menu(user.id if user else None)
    
    welcome_gif = get_bot_setting("welcome_gif", "")
    
    if update.callback_query:
        await update.callback_query.answer()
        cmsg = update.callback_query.message
        
        has_media = bool(cmsg.video or cmsg.document or cmsg.animation or cmsg.photo)
        is_photo = bool(cmsg.photo)

        if welcome_gif:
            # We want the welcome media. If current message is text OR a Photo (like the QR code), swap it out.
            if not has_media or is_photo:
                try:
                    await cmsg.delete()
                except Exception:
                    pass
                return await context.bot.send_animation(
                    chat_id=cmsg.chat_id, animation=welcome_gif, caption=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
                )
            else:
                try:
                    await update.callback_query.edit_message_caption(caption=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass
        else:
            # We want plain text. If current is any media, swap it out.
            if has_media:
                try:
                    await cmsg.delete()
                except Exception:
                    pass
                return await context.bot.send_message(
                    chat_id=cmsg.chat_id, text=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
                )
            else:
                try:
                    await update.callback_query.edit_message_text(text=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass
    else:
        if welcome_gif:
            try:
                await update.message.reply_animation(animation=welcome_gif, caption=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(text=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(
                text=WELCOME_MSG, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
            )


# ── Command: /random ───────────────────────────────────────────────────────


async def random_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context):
        return
        
    if not _is_admin(update) and not is_user_premium(update.effective_user.id):
        if not increment_quota(update.effective_user.id):
            quota = get_user_quota(update.effective_user.id)
            text = (
                "🚫 *You've used all your free watches!*\n\n"
                f"📊 Used: {quota['used']}/{quota['limit']}\n\n"
                "💡 *Get more:*\n"
                "🔗 Refer friends to earn +10 each\n"
                "👑 Or grab Premium for unlimited access\n"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Refer & Earn", callback_data="refer")],
                [InlineKeyboardButton("👑 Get Premium", callback_data="get_premium")],
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")],
            ])
            if update.callback_query:
                try:
                    await update.callback_query.answer("⚠️ Free completely used up!", show_alert=True)
                except Exception:
                    pass
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=text, 
                reply_markup=markup, 
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # Don't pre-answer — let _enrich_and_send handle it (or error handler on failure)
    limit = get_user_size_limit(update.effective_user.id) if update.effective_user else None
    row = db_random(limit)
    if not row:
        text = "🥺 NoDick is empty. Send a video or use /import."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, reply_markup=main_menu(update.effective_user.id)
            )
        else:
            await update.message.reply_text(
                text, reply_markup=main_menu(update.effective_user.id)
            )
        return

    log.info("random_video: calling _enrich_and_send for video_id=%s", row["id"])
    await _enrich_and_send(update, context, row["id"])
    log.info("random_video: _enrich_and_send returned")


# ── Command: /search ───────────────────────────────────────────────────────


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /search <keyword>")
        return
    query = " ".join(context.args)
    await _show_search(update, context, query=query, page=0)


async def search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context):
        return
    await update.callback_query.answer()
    context.user_data["waiting_for_search"] = True
    await _replace_with_text(update, context, "🔍 Send me a search keyword:", back())


async def _show_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str,
    page: int = 0,
):
    per_page = 10
    limit = get_user_size_limit(update.effective_user.id) if update.effective_user else None
    rows, total = search_videos(query, page=page, per_page=per_page, max_size_mb=limit)

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
        await _replace_with_text(update, context, text, markup)
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
    try:
        if not _is_admin(update):
            if update.callback_query:
                await update.callback_query.answer("❌ Admin only.", show_alert=True)
            return
            
        if await _check_force_join(update, context):
            return
        if update.callback_query:
            await update.callback_query.answer()
        total = db_video_count()
        views = total_views()
        cats = category_count()
        users = get_user_count()
        text = (
            f"📊 *NoDick Stats*\n\n"
            f"👥 Users: {users:,}\n"
            f"📹 Videos: {total:,}\n"
            f"👁 Views: {views:,}\n"
            f"📁 Categories: {cats:,}"
        )
        if update.callback_query:
            log.info("Stats: editing message %s for user %s",
                     update.callback_query.message.message_id, update.effective_user.id)
            await _replace_with_text(update, context, text, back())
            log.info("Stats: edit successful")
        else:
            await update.message.reply_text(
                text, reply_markup=main_menu(update.effective_user.id)
            )
    except Exception as e:
        log.error("Stats handler error: %s | %s", e, repr(e), exc_info=True)
        if update and update.callback_query:
            try:
                await update.callback_query.answer(f"⚠️ {str(e)[:50]}", show_alert=True)
            except Exception:
                pass


# ── Command: /categories ──────────────────────────────────────────────────


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context):
        return
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
        await _replace_with_text(update, context, text, markup)
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
    limit = get_user_size_limit(update.effective_user.id) if update.effective_user else None
    rows, total = get_videos_by_category(category, page=page, per_page=per_page, max_size_mb=limit)
    if not rows:
        await _replace_with_text(update, context, "🥺 No videos.", back())
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

    await _replace_with_text(update, context, f"📁 {category} — {len(rows)} of {total}", InlineKeyboardMarkup(buttons))


# ── Command: /favorites ────────────────────────────────────────────────────


async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context):
        return
    q = update.callback_query
    user_id = update.effective_user.id
    await q.answer()

    page = 0
    if q.data.startswith("favpage_"):
        page = int(q.data.split("_", 1)[1])

    per_page = 10
    rows, total = get_favorites(
        user_id, page=page, per_page=per_page,
        max_size_mb=get_user_size_limit(user_id),
    )
    if not rows:
        await _replace_with_text(update, context, "⭐ No favorites yet. Tap 💦 on a video to save it.", main_menu(user_id))
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

    await _replace_with_text(update, context, f"⭐ Favorites — {len(rows)} of {total}", InlineKeyboardMarkup(buttons))


async def add_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⭐ Toggled!")
    video_id = int(q.data.split("_", 1)[1])
    from nodick.db import toggle_favorite

    toggle_favorite(update.effective_user.id, video_id)


# ── Play (callback from inline buttons) ────────────────────────────────────


async def play_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context):
        return
        
    if not _is_admin(update) and not is_user_premium(update.effective_user.id):
        if not increment_quota(update.effective_user.id):
            quota = get_user_quota(update.effective_user.id)
            text = (
                "🚫 *You've used all your free watches!*\n\n"
                f"📊 Used: {quota['used']}/{quota['limit']}\n\n"
                "💡 *Get more:*\n"
                "🔗 Refer friends to earn +10 each\n"
                "👑 Or grab Premium for unlimited access\n"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Refer & Earn", callback_data="refer")],
                [InlineKeyboardButton("👑 Get Premium", callback_data="get_premium")],
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")],
            ])
            if update.callback_query:
                try:
                    await update.callback_query.answer("⚠️ Free completely used up!", show_alert=True)
                except Exception:
                    pass
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=text, 
                reply_markup=markup, 
                parse_mode=ParseMode.MARKDOWN
            )
            return

    q = update.callback_query
    await q.answer("Loading...")
    video_id = int(q.data.split("_", 1)[1])
    await _enrich_and_send(update, context, video_id, allow_redirect=False)


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
        await _replace_with_text(update, context, "⚙️ *Settings*", settings_keyboard())
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
        await q.edit_message_text("⚙️ Settings toggled.", reply_markup=settings_keyboard())
    elif setting == "autodelete":
        current = get_bot_setting("auto_delete_enabled", "1")
        set_bot_setting("auto_delete_enabled", "0" if current == "1" else "1")
        await q.edit_message_text("⚙️ Settings toggled.", reply_markup=settings_keyboard())
    elif setting == "autopost":
        current = get_bot_setting("auto_post_enabled", "0")
        set_bot_setting("auto_post_enabled", "0" if current == "1" else "1")
        await q.edit_message_text("⚙️ Auto-Post Task toggled.", reply_markup=settings_keyboard())


# ── Quality (max file size) filter ─────────────────────────────────────────



async def prompt_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        return
        
    action = q.data.replace("prompt_", "")
    
    if action == "autopostnow":
        from nodick.services.poster import send_auto_post
        channel_id = get_bot_setting("auto_post_channel", "-1004422688523")
        await q.edit_message_text(f"🚀 Triggering post to {channel_id}...", reply_markup=back())
        success = await send_auto_post(context.bot, channel_id)
        if success:
            await context.bot.send_message(update.effective_chat.id, "✅ Auto-Post successfully dropped in the channel.")
        else:
            await context.bot.send_message(update.effective_chat.id, "❌ Failed. Make sure bot is admin in the channel and has access to fetch videos.")
        return

    context.user_data["waiting_for_setting"] = action
    
    prompts = {
        "autodelete_timer": "Send me the new *Auto-Delete timer* in minutes (e.g. `30`):",
        "payment": "Send me the new *Payment Info* text (supports UPI, links, etc.):",
        "paymentqr": "Send me a *Photo* of your QR code:\n\n_(Send `clear` to remove an existing QR)_",
        "refbonus": "Send me the new *Referral Bonus* amount (e.g. `5`):",
        "logschannel": "Send me the new *Logs Channel ID* (e.g. `-1001234567890`):",
        "forcejoin": "Send me the *Channel/Group IDs* separated by spaces (e.g. `-100123 -100456`):\n\n_(Send `clear` to disable Force Join)_",
        "coadmins": "Send me the *Admin IDs* separated by spaces (e.g. `12345 67890`):\n\n_(Send `clear` to remove all extra admins)_",
        "grantpremium": "Send me the *User IDs* you want to grant 30-Day Premium to, separated by spaces (e.g. `12345 67890`):",
        "revokepremium": "Send me the *User IDs* you want to revoke Premium from, separated by spaces:",
        "grantadmin": "Send me the *User IDs* to grant Admin rights to, separated by spaces:",
        "revokeadmin": "Send me the *Admin IDs* to revoke Admin rights from, separated by spaces:",
        "welcomegif": "Send a URL to a GIF or Video to serve as the Welcome Banner (e.g. a Tenor link):\n\n_(Send `clear` to disable)_",
        "refergif": "Send a URL to a GIF or Video to serve as the Referral Banner (e.g. a Tenor link):\n\n_(Send `clear` to disable)_",
        "broadcast": "Send the message you want to broadcast to ALL your bot users:\n\n_(Standard text and Telegram formatting allowed)_",
    }
    
    await _replace_with_text(update, context, f"⚙️ {prompts.get(action, 'Send new value:')}\n\n_(Send your answer down below, or send /cancel to abort)_", back())

async def quality_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        await q.edit_message_text("❌ Admin only.", reply_markup=back())
        return
    await q.edit_message_text(
        "🎬 *Quality* — only show videos under your size cap\n\n"
        "Videos whose size isn't known yet pass through until backfill catches them.",
        reply_markup=quality_keyboard(update.effective_user.id),
        parse_mode="Markdown",
    )


async def quality_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        await q.edit_message_text("❌ Admin only.", reply_markup=back())
        return
    mb = int(q.data.split("_", 1)[1])  # quality_500 -> 500, quality_0 -> 0
    set_user_size_limit(update.effective_user.id, mb or None)
    await q.edit_message_text(
        "🎬 Quality updated.", reply_markup=quality_keyboard(update.effective_user.id)
    )


async def _lazy_fill_size(video_id: int, file_ref: str) -> None:
    """Fire-and-forget: fetch size for a channel_ref video via Telethon."""
    try:
        if not file_ref.startswith("channel_ref:"):
            return
        _, channel_id, message_id = file_ref.split(":", 2)
        client = await _get_telethon_bot()
        msgs = await client.get_messages(
            int(channel_id), ids=[int(message_id)]
        )
        msg = msgs[0] if isinstance(msgs, list) and msgs else msgs
        if not msg:
            return
        doc = getattr(msg, "document", None) or getattr(msg, "video", None)
        size = getattr(doc, "size", None)
        if size:
            update_video_size(video_id, int(size))
            log.info("Lazy size fill: video_id=%s -> %s bytes", video_id, size)
    except Exception as e:
        log.debug("Lazy size fill failed for video_id=%s: %s", video_id, e)


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
    """Import button pressed — immediately kick off import_scan if we know the start ID,
    otherwise prompt the user to forward the latest video from their channel."""
    q = update.callback_query
    if not _is_admin(update):
        await q.answer("Admin only", show_alert=True)
        return
    await q.answer()

    from nodick.db import get_bot_setting
    channel_str = get_bot_setting("scan_channel_id") or settings.default_import_channel
    if not channel_str:
        await _replace_with_text(update, context, "⚠️ No default channel set.\n\nUse /setchannel <channel_id> to set one, then press Import again.", back())
        return

    try:
        channel_id = int(channel_str)
    except ValueError:
        await _replace_with_text(update, context, f"⚠️ Invalid channel ID stored: {channel_str!r}\nUse /setchannel <channel_id> to fix it.", back())
        return

    last_msg_id_str = get_bot_setting("scan_last_message_id")
    if last_msg_id_str:
        # We have a start ID from a previous scan — fire immediately
        start_id = int(last_msg_id_str)
        await _replace_with_text(update, context, f"🛰 Resuming scan of `{channel_id}` from message `{start_id}`...", scan_running_keyboard())
        await _start_message_import(update, context, channel_id, start_id)
    else:
        # No start ID yet — ask user to forward the latest video
        await _replace_with_text(update, context, f"🛰 Channel set: `{channel_id}`\n\nForward the *latest* video from your channel to me and I'll start scanning automatically.\n\n_(Only needed once — I'll remember the position after that.)_", back())


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
        await _replace_with_text(update, context, text, import_keyboard())
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


# ── Command: /setchannel ──────────────────────────────────────────────────


async def setchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: set the default scan channel.
    Usage: /setchannel <channel_id>
    Clears the saved start message ID so next Import starts fresh from a forward.
    """
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        current = get_bot_setting("scan_channel_id") or settings.default_import_channel or "not set"
        await update.message.reply_text(
            f"Current scan channel: `{current}`\n\n"
            "Usage: /setchannel <channel_id>",
            parse_mode="Markdown",
        )
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ channel_id must be an integer (e.g., -1001234567890)")
        return
    set_bot_setting("scan_channel_id", str(channel_id))
    set_bot_setting("scan_last_message_id", "")  # reset so user forwards again to pick start
    await update.message.reply_text(
        f"✅ Default scan channel set to `{channel_id}`.\n\n"
        "Now forward the latest video from that channel to me and press 🛰 Import.",
        parse_mode="Markdown",
    )


# ── Forwarded message handler ──────────────────────────────────────────────


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

    # Persist channel + latest message ID so Import button can auto-resume next time
    from nodick.db import set_bot_setting
    set_bot_setting("scan_channel_id", str(channel_id))
    set_bot_setting("scan_last_message_id", str(message_id))

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
    msg = update.message
    text = msg.text or msg.caption or ""
    text = text.strip()
    
    if text.lower() == "/cancel":
        context.user_data.pop("waiting_for_search", None)
        context.user_data.pop("waiting_for_correction", None)
        context.user_data.pop("waiting_for_setting", None)
        await update.message.reply_text("❌ Action cancelled.", reply_markup=main_menu(update.effective_user.id))
        return

    # Check if waiting for a setting update
    setting = context.user_data.pop("waiting_for_setting", None)
    if setting:
        if setting == "paymentqr":
            if msg.photo:
                set_bot_setting("payment_qr", msg.photo[-1].file_id)
                await msg.reply_text("✅ Payment QR Code updated!", reply_markup=settings_keyboard(), parse_mode=ParseMode.MARKDOWN)
            elif text.lower() == "clear":
                set_bot_setting("payment_qr", "")
                await msg.reply_text("✅ Payment QR removed.", reply_markup=settings_keyboard(), parse_mode=ParseMode.MARKDOWN)
            else:
                await msg.reply_text("❌ Please send a PHOTO (image), or send `clear` to remove it.", reply_markup=settings_keyboard(), parse_mode=ParseMode.MARKDOWN)
            return

        if setting == "autodelete_timer":
            try:
                n = int(text)
                set_bot_setting("auto_delete_minutes", str(n))
                msg = f"✅ Auto-delete timer set to {n} minutes."
            except ValueError:
                msg = "❌ Must be a number."
        elif setting == "payment":
            set_bot_setting("payment_info", text)
            msg = "✅ Payment info updated."
        elif setting == "refbonus":
            try:
                n = int(text)
                set_bot_setting("referral_bonus", str(n))
                msg = f"✅ Referral bonus set to +{n} watches."
            except ValueError:
                msg = "❌ Must be a number."
        elif setting == "logschannel":
            set_bot_setting("logs_channel_id", text)
            msg = f"✅ Logs channel set to `{text}`."
        elif setting == "forcejoin":
            if text.lower() == "clear":
                set_bot_setting("force_join_channels", "[]")
                msg = "✅ Force Join disabled."
            else:
                ids = text.split()
                channels = []
                failed = []
                for cid in ids:
                    try:
                        chat_id = int(cid)
                        # Fetch chat automatically
                        chat = await context.bot.get_chat(chat_id)
                        url = chat.invite_link
                        if not url:
                            url = await context.bot.export_chat_invite_link(chat_id)
                        channels.append({"id": chat_id, "name": chat.title or str(chat_id), "url": url})
                    except Exception as e:
                        failed.append(f"`{cid}`: {str(e)}")
                
                if channels:
                    import json
                    from nodick.db import get_bot_setting
                    set_bot_setting("force_join_channels", json.dumps(channels))
                    msg = f"✅ Saved {len(channels)} channels for Force Join."
                else:
                    msg = "❌ No valid channels found.\nMake sure the bot is an admin in those channels to generate invite links."
                    
                if failed:
                    msg += "\n\n⚠️ Failed:\n" + "\n".join(failed)
        elif setting == "grantpremium":
            ids = text.split()
            count, failed = 0, 0
            for cid_str in ids:
                try:
                    cid = int(cid_str)
                    grant_premium(cid, 30)
                    try:
                        promo_text = (
                            "👑 *PREMIUM UNLOCKED* 👑\n\n"
                            "The admin has personally upgraded your account to *Premium for 30 Days*! 🔥\n\n"
                            "🔓 Unlimited Surprise Me accesses\n"
                            "🚫 Zero restrictions natively unlocked\n\n"
                            "Go wild. 😏"
                        )
                        await context.bot.send_message(chat_id=cid, text=promo_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass
                    count += 1
                except Exception:
                    failed += 1
            msg = f"✅ Granted 30 Days Premium to {count} users.\n(Failed/Skipped: {failed})"
        elif setting == "revokepremium":
            ids = text.split()
            count, failed = 0, 0
            for cid in ids:
                try:
                    revoke_premium(int(cid))
                    count += 1
                except Exception:
                    failed += 1
            msg = f"✅ Revoked Premium from {count} users.\n(Failed/Skipped: {failed})"
        elif setting == "broadcast":
            user_ids = get_all_user_ids()
            
            async def _run_bg_broadcast():
                sent, dropped = 0, 0
                for uid in user_ids:
                    try:
                        await context.bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.MARKDOWN)
                        sent += 1
                    except Exception:
                        dropped += 1
                    await asyncio.sleep(0.04) # Flood protection
                # Log completion to logs channel
                ch_id = get_bot_setting("logs_channel_id", "")
                if ch_id:
                    try:
                        await context.bot.send_message(chat_id=int(ch_id), text=f"📡 Broadcast Complete!\n✅ Sent: {sent}\n❌ Failed: {dropped}")
                    except: pass
            
            context.application.create_task(_run_bg_broadcast())
            msg = f"📡 Broadcast queued for {len(user_ids)} users! You'll get a notification in your Logs Channel when it finishes."
            
        elif setting == "grantadmin":
            ids = text.split()
            current = (get_bot_setting("extra_admins", "") or "").split()
            added = 0
            for cid in ids:
                if cid.isdigit() and cid not in current:
                    current.append(cid)
                    added += 1
            set_bot_setting("extra_admins", " ".join(current))
            msg = f"✅ Granted Admin to {added} users."
        elif setting == "revokeadmin":
            ids = text.split()
            current = (get_bot_setting("extra_admins", "") or "").split()
            removed = 0
            new_list = []
            for cid in current:
                if cid in ids:
                    removed += 1
                else:
                    new_list.append(cid)
            set_bot_setting("extra_admins", " ".join(new_list))
            msg = f"✅ Revoked Admin from {removed} users."
        elif setting == "welcomegif":
            if text.lower() == "clear":
                set_bot_setting("welcome_gif", "")
                msg = "✅ Welcome GIF disabled."
            else:
                set_bot_setting("welcome_gif", text)
                msg = "✅ Welcome GIF updated!"
                
        elif setting == "refergif":
            if text.lower() == "clear":
                set_bot_setting("refer_gif", "")
                msg = "✅ Referral GIF disabled."
            else:
                set_bot_setting("refer_gif", text)
                msg = "✅ Referral GIF updated!"
        
        await update.message.reply_text(msg, reply_markup=settings_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return

    if not text:
        return

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




# ── Feature Admin Commands ───────────────────────────────────────────────

async def forcejoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: manage forced join channels."""
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "📢 *Force Join Channels*\n\n"
            "Usage:\n"
            "`/forcejoin add <channel_id> <name> <url>`\n"
            "`/forcejoin remove <channel_id>`\n"
            "`/forcejoin list`\n\n"
            "Example:\n"
            "`/forcejoin add -1001234567890 MyChannel https://t.me/mychannel`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    sub = context.args[0].lower()
    channels = _get_force_join_channels()
    if sub == "list":
        if not channels:
            await update.message.reply_text("📢 No forced join channels configured.")
            return
        lines = ["📢 *Force Join Channels:*\n"]
        for ch in channels:
            lines.append(f"• `{ch['id']}` — [{ch['name']}]({ch['url']})")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    elif sub == "add":
        if len(context.args) < 4:
            await update.message.reply_text(
                "Usage: `/forcejoin add <channel_id> <name> <url>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            ch_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ channel_id must be an integer.")
            return
        ch_name = context.args[2]
        ch_url = context.args[3]
        if any(c["id"] == ch_id for c in channels):
            await update.message.reply_text(f"⚠️ Channel `{ch_id}` already in list.", parse_mode=ParseMode.MARKDOWN)
            return
        channels.append({"id": ch_id, "name": ch_name, "url": ch_url})
        set_bot_setting("force_join_channels", json.dumps(channels))
        await update.message.reply_text(
            f"✅ Added force join channel: *{ch_name}* (`{ch_id}`)",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif sub == "remove":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/forcejoin remove <channel_id>`", parse_mode=ParseMode.MARKDOWN)
            return
        try:
            ch_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ channel_id must be an integer.")
            return
        before = len(channels)
        channels = [c for c in channels if c["id"] != ch_id]
        if len(channels) == before:
            await update.message.reply_text(f"⚠️ Channel `{ch_id}` not found.", parse_mode=ParseMode.MARKDOWN)
            return
        set_bot_setting("force_join_channels", json.dumps(channels))
        await update.message.reply_text(
            f"✅ Removed channel `{ch_id}` from force join list.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("❌ Unknown subcommand. Use `add`, `remove`, or `list`.", parse_mode=ParseMode.MARKDOWN)

async def setad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    raw = update.message.text
    _, _, payload = raw.partition(" ")
    payload = payload.strip()
    if not payload or "|" not in payload:
        await update.message.reply_text(
            "📣 *Set In-Bot Ad*\n\n"
            "Usage: `/setad text | button_text | button_url`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    parts = [p.strip() for p in payload.split("|")]
    if len(parts) != 3:
        await update.message.reply_text("❌ Need exactly 3 pipe-separated values: `text | button_text | button_url`", parse_mode=ParseMode.MARKDOWN)
        return
    ad_text, button_text, button_url = parts
    config = {"text": ad_text, "button_text": button_text, "button_url": button_url}
    set_bot_setting("ad_config", json.dumps(config))
    freq = _get_ad_frequency()
    await update.message.reply_text(
        f"✅ Ad configured!\n\n📝 Text: {ad_text}\n🔘 Button: [{button_text}]({button_url})\n📊 Frequency: every {freq} videos",
        parse_mode=ParseMode.MARKDOWN,
    )

async def clearad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    set_bot_setting("ad_config", "")
    await update.message.reply_text("✅ Ad cleared. No ads will be shown.")

async def adfreq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return
    if not context.args:
        current = _get_ad_frequency()
        await update.message.reply_text(
            f"📊 Current ad frequency: every *{current}* videos\n\nUsage: `/adfreq <number>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        n = int(context.args[0])
        if n < 1:
            await update.message.reply_text("❌ Frequency must be at least 1.")
            return
    except ValueError:
        await update.message.reply_text("❌ Provide a number.")
        return
    set_bot_setting("ad_frequency", str(n))
    await update.message.reply_text(f"✅ Ad frequency set to every *{n}* videos.", parse_mode=ParseMode.MARKDOWN)
    
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
    """Log errors and answer callback queries so buttons don't hang."""
    err = context.error
    update_id = update.update_id if update else None
    cb_data = None
    if update and update.callback_query:
        cb_data = update.callback_query.data
        try:
            await update.callback_query.answer(
                "⚠️ Something broke — check logs" if cb_data else None,
                show_alert=True,
            )
        except Exception:
            pass
    log.error("⚠️ Error: %s | type=%s | repr=%s | update_id=%s callback=%s",
              err, type(err).__name__, repr(err), update_id, cb_data)


# ── No-op callback (for non-interactive info buttons) ──────────────────────


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently acknowledge non-interactive buttons (e.g., part counter)."""
    await update.callback_query.answer()


# ── Smart recommendations: More Like This + Cast ───────────────────────────


def _b64(s: str) -> str:
    # Standard base64 (not urlsafe): output has no '_' so it can't collide
    # with the '_'-delimited callback_data format (perf_<enc> / perfpage_<enc>_<page>).
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _unb64(s: str) -> str:
    return base64.b64decode(s.encode("ascii")).decode("utf-8")


async def similar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🎯 More Like This — videos sharing tags/performers with the current one."""
    q = update.callback_query
    try:
        video_id = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await q.answer("Hmm?", show_alert=True)
        return
    sims = get_similar_videos(video_id, limit=6)
    if not sims:
        await q.answer(
            "No close matches yet — cache is still growing, keep watching 😏",
            show_alert=True,
        )
        return
    rows = []
    for s in sims:
        title = (s.get("title") or f"Video {s['id']}")[:42]
        rows.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"play_{s['id']}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    await q.answer()
    await q.message.reply_text(
        "🎯 *More like this*",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """👤 Cast — pick a performer from the current video's cached metadata."""
    q = update.callback_query
    try:
        video_id = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await q.answer("Hmm?", show_alert=True)
        return
    meta = get_video_metadata(video_id)
    performers = (
        [p for p in (dict(meta).get("stashdb_performer") or "").split(",") if p]
        if meta else []
    )
    if not performers:
        await q.answer("No cast cached yet — keep watching 😏", show_alert=True)
        return
    rows = []
    for name in performers[:6]:
        try:
            n = performer_video_count(name)
        except Exception:
            n = 0
        enc = _b64(name)
        # Telegram callback_data is capped at 64 bytes — skip names that
        # would overflow ("perfpage_<enc>_<page>" is the longest form).
        if len(f"perfpage_{enc}_0") > 64:
            continue
        label = f"👤 {name}" + (f" ({n})" if n else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"perf_{enc}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    await q.answer()
    await q.message.reply_text(
        "👤 *Cast*",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )


async def performer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filmography for a performer — paginated list of their videos."""
    q = update.callback_query
    data = q.data
    try:
        if data.startswith("perfpage_"):
            _, enc, page_str = data.split("_", 2)
            page = int(page_str)
        else:
            _, enc = data.split("_", 1)
            page = 0
        name = _unb64(enc)
    except Exception:
        await q.answer("Hmm?", show_alert=True)
        return
    rows, total = get_videos_by_performer(name, page=page, per_page=8)
    if not rows:
        await q.answer("Nothing here yet", show_alert=True)
        return
    buttons = []
    for r in rows:
        title = (r.get("title") or f"Video {r['id']}")[:42]
        buttons.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"play_{r['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"perfpage_{enc}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{(total + 7) // 8}", callback_data="noop"))
    if (page + 1) * 8 < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"perfpage_{enc}_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    await q.answer()
    await q.message.edit_text(
        f"👤 {name} — {total} in your library",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _catchall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch any callback queries that didn't match a specific handler."""
    q = update.callback_query
    log.warning("⚠️ Unhandled callback: %s | from_user=%s", q.data, q.from_user.id if q.from_user else "?")
    await q.answer("⏳ This button isn't wired up yet", show_alert=False)




# ── Premium Admin Commands ────────────────────────────────────────────────

async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/grant <user_id> [days]`\n"
            "No days = lifetime premium",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return
    days = None
    if len(context.args) > 1:
        try:
            days = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ days must be a number.")
            return
    grant_premium(target_id, days)
    duration = f"{days} days" if days else "lifetime"
    await update.message.reply_text(
        f"✅ Premium granted to `{target_id}` ({duration})",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _log_event(context, f"👑 Premium granted to `{target_id}` ({duration})")
    
    try:
        if days:
            promo_text = (
                "👑 *PREMIUM UNLOCKED* 👑\n\n"
                f"Your account has been upgraded to *Premium* for {days} days! 🔥\n\n"
                "🔓 Unlimited Surprise Me accesses\n"
                "🚫 Zero restrictions natively unlocked\n\n"
                "Go wild. 😏"
            )
        else:
            promo_text = (
                "👑 *PREMIUM UNLOCKED* 👑\n\n"
                "The admin has personally upgraded your account to *Lifetime Premium*! 🔥\n\n"
                "🔓 Unlimited Surprise Me accesses\n"
                "🚫 Zero restrictions natively unlocked\n\n"
                "Go wild. 😏"
            )
        await context.bot.send_message(chat_id=target_id, text=promo_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/revoke <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return
    revoke_premium(target_id)
    await update.message.reply_text(f"✅ Premium revoked from `{target_id}`", parse_mode=ParseMode.MARKDOWN)
    await _log_event(context, f"❌ Premium revoked from `{target_id}`")

async def premiumlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    users = list_premium_users()
    if not users:
        await update.message.reply_text("No premium users.")
        return
    lines = ["👑 *Premium Users:*\n"]
    for u in users:
        until = u.get('premium_until') or 'Lifetime'
        lines.append(f"• `{u['user_id']}` — until {until}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def setpayment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    raw = update.message.text
    _, _, payload = raw.partition(" ")
    if not payload.strip():
        current = get_bot_setting("payment_info", "Not set")
        await update.message.reply_text(f"Current payment info:\n{current}\n\nUsage: /setpayment <text>")
        return
    set_bot_setting("payment_info", payload.strip())
    await update.message.reply_text("✅ Payment info updated.")

async def setrefbonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        current = get_bot_setting("referral_bonus", "5")
        await update.message.reply_text(f"Current referral bonus: {current}\n\nUsage: /setrefbonus <number>")
        return
    try:
        n = int(context.args[0])
        if n < 1:
            await update.message.reply_text("❌ Must be at least 1.")
            return
    except ValueError:
        await update.message.reply_text("❌ Provide a number.")
        return
    set_bot_setting("referral_bonus", str(n))
    await update.message.reply_text(f"✅ Referral bonus set to +{n} watches per referral.")

async def setlogs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        current = get_bot_setting("logs_channel_id", "") or str(settings.logs_channel_id) or "Not set"
        await update.message.reply_text(f"Current logs channel: `{current}`\n\nUsage: `/setlogs <channel_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        ch_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Must be an integer.")
        return
    set_bot_setting("logs_channel_id", str(ch_id))
    await update.message.reply_text(f"✅ Logs channel set to `{ch_id}`", parse_mode=ParseMode.MARKDOWN)

async def setautodelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        mins = get_bot_setting("auto_delete_minutes", "30")
        enabled = get_bot_setting("auto_delete_enabled", "1")
        status = "ON" if enabled == "1" else "OFF"
        await update.message.reply_text(f"Auto-delete: {status}, timer: {mins} min\n\nUsage: /setautodelete <minutes>")
        return
    try:
        n = int(context.args[0])
        if n < 1:
            await update.message.reply_text("❌ Must be at least 1 minute.")
            return
    except ValueError:
        await update.message.reply_text("❌ Provide a number.")
        return
    set_bot_setting("auto_delete_minutes", str(n))
    await update.message.reply_text(f"✅ Auto-delete timer set to {n} minutes.")

async def autodelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        enabled = get_bot_setting("auto_delete_enabled", "1")
        status = "ON" if enabled == "1" else "OFF"
        await update.message.reply_text(f"Auto-delete is: {status}\n\nUsage: /autodelete on|off")
        return
    val = context.args[0].lower()
    if val in ("on", "1", "yes"):
        set_bot_setting("auto_delete_enabled", "1")
        await update.message.reply_text("✅ Auto-delete enabled.")
    elif val in ("off", "0", "no"):
        set_bot_setting("auto_delete_enabled", "0")
        await update.message.reply_text("✅ Auto-delete disabled.")
    else:
        await update.message.reply_text("Usage: /autodelete on|off")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    raw = update.message.text
    _, _, payload = raw.partition(" ")
    if not payload.strip():
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    user_ids = get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await update.message.reply_text(f"📡 Broadcasting to {len(user_ids)} users...")
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=payload, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 25 == 0:
            try:
                await status_msg.edit_text(f"📡 Broadcasting... {sent + failed}/{len(user_ids)}")
            except Exception:
                pass
    await status_msg.edit_text(f"📡 Broadcast complete!\n✅ Sent: {sent}\n❌ Failed: {failed}")

async def my_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    from nodick.telegram.keyboards import account_keyboard
    await _replace_with_text(update, context, "👤 *My Account*", account_keyboard(user_id))

async def my_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    quota = get_user_quota(user_id)
    ref_count = get_referral_count(user_id)
    premium = is_user_premium(user_id)
    
    status = "👑 Premium" if premium else "🆓 Free"
    text = (
        f"📊 *Your Stats*\n\n"
        f"Status: {status}\n"
        f"Videos watched: {quota['used']}\n"
    )
    if not premium:
        total_blocks = 10
        if quota['limit'] > 0:
            filled = min(total_blocks, int(total_blocks * quota['remaining'] / quota['limit']))
        else:
            filled = total_blocks
        empty = total_blocks - filled
        bar = f"[{'▮' * filled}{'▯' * empty}]"
        text += f"Daily Quota: `{bar}` {quota['remaining']}/{quota['limit']}\n"
    text += f"Referrals: {ref_count}\n"
    
    from nodick.telegram.keyboards import account_keyboard
    await _replace_with_text(update, context, text, account_keyboard(user_id))

async def refer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    ref_count = get_referral_count(user_id)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    bonus = int(get_bot_setting("referral_bonus", "10"))
    
    progress = ref_count % 10
    needed = 10 - progress
    
    text = (
        f"🔗 *Refer & Earn*\n\n"
        f"Share your link to unlock free watches AND Premium! 🎁\n\n"
        f"• Earn *+{bonus} watches* per friend.\n"
        f"• *1 Month Premium* for every 10 referrals!\n"
        f"  _(You need {needed} more for your next Premium reward)_\n\n"
        f"Your link:\n`{ref_link}`\n\n"
        f"👥 Friends referred: *{ref_count}*\n\n"
        f"_They join, you get spoiled. Simple._ 😏"
    )
    
    refer_gif = get_bot_setting("refer_gif", "")
    
    if refer_gif:
        q = update.callback_query
        try:
            await q.message.delete()
        except Exception:
            pass
            
        try:
            await context.bot.send_animation(
                chat_id=update.effective_chat.id,
                animation=refer_gif,
                caption=text,
                reply_markup=refer_keyboard(ref_link),
                parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception:
            pass # fallback to text below if animation fails
            
    await _replace_with_text(update, context, text, refer_keyboard(ref_link))

async def get_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    payment_info = get_bot_setting("payment_info", "")
    if not payment_info or payment_info == "Contact admin for payment details." or payment_info == "Not set":
        payment_info = f"UPI: `razerx@ptaxis`\n\n_Send the screenshot directly to_ 💎 @TheRazerhub _and I'll activate it within minutes._ 🔥"
    
    text = (
        f"👑 *Premium Access*\n\n"
        f"🔓 Unlimited video watches\n"
        f"⚡ No restrictions\n"
        f"💰 Just ₹50 — 1 Month\n\n"
        f"*How to pay:*\n"
        f"{payment_info}"
    )
    
    qr_id = get_bot_setting("payment_qr", "")
    if qr_id:
        try:
            await q.message.delete()
        except Exception:
            pass
        await context.bot.send_photo(
            chat_id=q.message.chat_id,
            photo=qr_id,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back()
        )
    else:
        await _replace_with_text(update, context, text, back())

async def grantadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return
    if not context.args:
        await update.message.reply_text("Usage: /grantadmin <user_id>")
        return
    ids = context.args
    current = (get_bot_setting("extra_admins", "") or "").split()
    added = 0
    for cid in ids:
        if cid.isdigit() and cid not in current:
            current.append(cid)
            added += 1
    if added:
        set_bot_setting("extra_admins", " ".join(current))
    await update.message.reply_text(f"✅ Granted Admin to {added} users.")

async def revokeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return
    if not context.args:
        await update.message.reply_text("Usage: /revokeadmin <user_id>")
        return
    ids = context.args
    current = (get_bot_setting("extra_admins", "") or "").split()
    removed = 0
    new_list = []
    for cid in current:
        if cid in ids:
            removed += 1
        else:
            new_list.append(cid)
    set_bot_setting("extra_admins", " ".join(new_list))
    await update.message.reply_text(f"✅ Revoked Admin from {removed} users.")

async def _log_event(context: ContextTypes.DEFAULT_TYPE, message: str):
    ch_id = get_bot_setting("logs_channel_id", "") or str(settings.logs_channel_id)
    if not ch_id or ch_id == "0":
        return
    try:
        await context.bot.send_message(
            chat_id=int(ch_id),
            text=f"📋 {message}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        log.debug("Failed to send log: %s", e)


# ── Application builder ────────────────────────────────────────────────────


def build_application() -> Application:
    init_db()
    app = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )

    # ── Job Queue ──
    async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
        from nodick.db import get_bot_setting
        enabled = get_bot_setting("auto_post_enabled", "0")
        if enabled == "1":
            channel_id = get_bot_setting("auto_post_channel", "-1004422688523")
            from nodick.services.poster import send_auto_post
            await send_auto_post(context.bot, channel_id)

    if app.job_queue:
        app.job_queue.run_repeating(auto_post_job, interval=3600, first=60)
    else:
        log.warning("JobQueue not initialized. Auto-post will not run.")

    # ── Commands ──
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("forcejoin", forcejoin_command))
    app.add_handler(CommandHandler("setad", setad_command))
    app.add_handler(CommandHandler("clearad", clearad_command))
    app.add_handler(CommandHandler("adfreq", adfreq_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("premiumlist", premiumlist_command))
    app.add_handler(CommandHandler("setpayment", setpayment_command))
    app.add_handler(CommandHandler("setrefbonus", setrefbonus_command))
    app.add_handler(CommandHandler("setlogs", setlogs_command))
    app.add_handler(CommandHandler("setautodelete", setautodelete_command))
    app.add_handler(CommandHandler("autodelete", autodelete_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("mystats", my_stats_callback))
    app.add_handler(CommandHandler("refer", refer_callback))
    app.add_handler(CommandHandler("premium", get_premium_callback))
    app.add_handler(CommandHandler("grantadmin", grantadmin_command))
    app.add_handler(CommandHandler("revokeadmin", revokeadmin_command))
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
    app.add_handler(CommandHandler("setchannel", setchannel_command))

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
    app.add_handler(CallbackQueryHandler(prompt_setting, pattern="^prompt_"))
    app.add_handler(CallbackQueryHandler(quality_menu, pattern="^quality$"))
    app.add_handler(CallbackQueryHandler(quality_set, pattern="^quality_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_rename_callback, pattern="^rename_"))
    app.add_handler(CallbackQueryHandler(handle_correct_callback, pattern="^correct_"))
    app.add_handler(CallbackQueryHandler(scanimport_callback, pattern="^scanimport_"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))
    # Smart recommendations: More Like This + Cast + performer filmography
    app.add_handler(CallbackQueryHandler(similar_callback, pattern="^similar_"))
    app.add_handler(CallbackQueryHandler(cast_callback, pattern="^cast_"))
    app.add_handler(CallbackQueryHandler(performer_callback, pattern="^perf"))
    # Monetization callbacks
    app.add_handler(CallbackQueryHandler(verify_join_callback, pattern="^verify_join$"))
    app.add_handler(CallbackQueryHandler(my_account, pattern="^my_account$"))
    app.add_handler(CallbackQueryHandler(my_stats_callback, pattern="^my_stats$"))
    app.add_handler(CallbackQueryHandler(refer_callback, pattern="^refer$"))
    app.add_handler(CallbackQueryHandler(get_premium_callback, pattern="^get_premium$"))

    # Catch-all for unhandled callback queries — log + answer so buttons don't freeze
    app.add_handler(CallbackQueryHandler(_catchall_callback))

    # ── Message handlers ──
    # Forwarded-from-channel detection must come BEFORE video handler
    # so it can offer batch import instead of single index
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forward))
    # Video handler needs to skip forwarded-from-channel messages
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.FORWARDED, handle_video))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, text_router))

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
