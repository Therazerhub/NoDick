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
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nodick.config import settings
from nodick.telegram.welcome import DEFAULT_WELCOME_MEDIA, banner_from_message, decode_banner
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
    refund_quota,
    bump_streak,
    get_user_streak,
    streak_bar,
    grant_premium,
    revoke_premium,
    list_premium_users,
    set_pending_referral,
    activate_pending_referral,
    get_referral_count,
    get_referral_bonus_total,
    get_referral_social_proof,
    get_or_assign_welcome_variant,
    get_welcome_variant,
    get_welcome_experiment_stats,
    mark_welcome_exposure,
    mark_welcome_conversion,
    get_all_user_ids,
    user_exists,
    get_user_count,
    record_growth_event,
    get_growth_metrics,
)
from nodick.services.importer import TelegramImporter
from nodick.services.message_importer import (
    MessageIDImporter,
    _get_telethon_client as _get_telethon_bot,
)
from nodick.telegram.keyboards import (
    back,
    import_menu as import_keyboard,
    scan_running_keyboard,
    main_menu,
    part_nav_row,
    quality_keyboard,
    settings_keyboard,
    video_actions,
    account_keyboard,
    referral_keyboard,
    force_join_keyboard,
    ad_button_keyboard,
)
from nodick.utils import (
    clean_title_for_display,
    extract_category_from_title,
    format_duration,
    is_placeholder_title,
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

WELCOME_MSG_A = """*Your private stream is ready.* 🖤

⚡ *{remaining} watches available*  `{quota_bar}`
🔥 *{streak}-day streak*

Pick what you want. One tap, instant delivery.

⚡ _Built by_ [The Razer](tg://user?id=6001922744)"""

WELCOME_MSG_B = """*{video_count:,}+ videos. Pick one.* 🖤

⚡ *{remaining} watches available*  `{quota_bar}`
🔥 *{streak}-day streak*

Try *🎲 Surprise Me* or search the library.

⚡ _Built by_ [The Razer](tg://user?id=6001922744)"""


def _welcome_message(user_id: int | None) -> str:
    """Return the user's stable onboarding experiment variant."""
    if user_id is None:
        return WELCOME_MSG_A.format(remaining=5, quota_bar="▮▮▮▮▮", streak=0)
    variant = get_welcome_variant(user_id)
    if variant:
        mark_welcome_exposure(user_id)
    quota = get_user_quota(user_id)
    streak = get_user_streak(user_id)["streak"]
    if quota["is_premium"]:
        remaining, quota_bar = "Unlimited", "▮▮▮▮▮"
    else:
        remaining = quota["remaining"]
        width = 5
        filled = min(width, round(width * quota["remaining"] / max(1, quota["limit"])))
        quota_bar = "▮" * filled + "▯" * (width - filled)
    values = {
        "video_count": db_video_count(),
        "remaining": remaining,
        "quota_bar": quota_bar,
        "streak": streak,
    }
    return (WELCOME_MSG_B if variant == "B" else WELCOME_MSG_A).format(**values)


def _parse_start_payload(payload: str | None) -> tuple[int | None, int | None]:
    """Return (video_id, referrer_id) for supported Telegram deep links."""
    if not payload:
        return None, None
    try:
        if payload.startswith("ref_"):
            referrer_id = int(payload.removeprefix("ref_"))
            return (None, referrer_id) if referrer_id > 0 else (None, None)
        if payload.startswith("vid_"):
            video_id = int(payload.removeprefix("vid_"))
            return (video_id, None) if video_id > 0 else (None, None)
        if payload.startswith("sv_"):
            _, raw_video_id, raw_referrer_id = payload.split("_", 2)
            video_id, referrer_id = int(raw_video_id), int(raw_referrer_id)
            if video_id > 0 and referrer_id > 0:
                return video_id, referrer_id
    except (ValueError, TypeError):
        pass
    return None, None


def _acquisition_source(payload: str | None) -> str:
    if not payload:
        return "organic"
    if payload.startswith("ref_"):
        return "referral"
    if payload.startswith("sv_"):
        return "shared_video"
    if payload.startswith("vid_"):
        return "video_link"
    return "other_deep_link"


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


async def _send_welcome(update, context, text, markup):
    """Send the selected banner, falling back to text without stranding the menu."""
    message = update.callback_query.message if update.callback_query else update.message
    sent = None
    try:
        banner = decode_banner(get_bot_setting("welcome_media", DEFAULT_WELCOME_MEDIA))
        if banner:
            kind = banner["type"]
            sender = getattr(context.bot, f"send_{kind}")
            sent = await sender(
                chat_id=message.chat_id, **{kind: banner["media"]},
                caption=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN,
            )
    except Exception:
        log.exception("Welcome banner unavailable; sending text menu")
    if sent is None:
        sent = await context.bot.send_message(
            chat_id=message.chat_id, text=text, reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
        )
    # Delete only after a replacement exists, including after force-join.
    if update.callback_query:
        try:
            await message.delete()
        except Exception:
            log.debug("Could not remove previous welcome/menu message", exc_info=True)
    return sent


async def welcome_media_input(update, context):
    """Claim admin banner uploads before forwarding/import/indexing handlers."""
    if context.user_data.get("waiting_for_setting") != "welcomegif":
        return
    if not _is_admin(update):
        context.user_data.pop("waiting_for_setting", None)
        raise ApplicationHandlerStop
    try:
        banner = banner_from_message(update.message)
        # Validate with Telegram before persisting; a broken URL keeps the old banner.
        if banner:
            kind = banner["type"]
            await getattr(context.bot, f"send_{kind}")(
                chat_id=update.effective_chat.id, **{kind: banner["media"]},
                caption="Welcome banner preview",
            )
        set_bot_setting("welcome_media", json.dumps(banner) if banner else "")
        context.user_data.pop("waiting_for_setting", None)
        await update.message.reply_text(
            "✅ Welcome media updated." if banner else "✅ Welcome media disabled.",
            reply_markup=settings_keyboard(),
        )
    except Exception as exc:
        log.warning("Welcome media update rejected: %s", exc)
        await update.message.reply_text(
            "Could not save that banner. Send a photo, GIF, MP4, or a direct media URL "
            "(not a Tenor/GIPHY page). Your previous banner is unchanged. /cancel to abort."
        )
    raise ApplicationHandlerStop


# ── Force Join check ──────────────────────────────────────────────────────

def _force_join_config(raw: str | None = None) -> dict:
    """Parse and validate force-join configuration without silently disabling it."""
    if raw is None:
        raw = get_bot_setting("force_join_channels", "[]")
    if raw in (None, "", "[]"):
        return {"enabled": False, "valid": True, "channels": [], "error": None}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"enabled": True, "valid": False, "channels": [], "error": "invalid JSON"}
    if not isinstance(value, list):
        return {"enabled": True, "valid": False, "channels": [], "error": "expected a list"}
    if not value:
        return {"enabled": False, "valid": True, "channels": [], "error": None}
    channels = []
    for index, channel in enumerate(value):
        if not isinstance(channel, dict):
            return {"enabled": True, "valid": False, "channels": [], "error": f"entry {index + 1} is not an object"}
        try:
            channel_id = int(channel["id"])
            name = str(channel["name"]).strip()
            url = str(channel["url"]).strip()
        except (KeyError, TypeError, ValueError):
            return {"enabled": True, "valid": False, "channels": [], "error": f"entry {index + 1} has invalid fields"}
        if not name or len(name) > 80 or not url.startswith("https://"):
            return {"enabled": True, "valid": False, "channels": [], "error": f"entry {index + 1} has unsafe display fields"}
        channels.append({"id": channel_id, "name": name, "url": url})
    return {"enabled": True, "valid": True, "channels": channels, "error": None}


def _get_force_join_channels() -> list[dict]:
    """Compatibility accessor; invalid enabled configuration returns no channels."""
    return _force_join_config()["channels"]


def _member_has_access(member) -> bool:
    """Classify Telegram membership conservatively, including restricted users."""
    status = str(getattr(member, "status", "")).lower()
    if status in {"member", "administrator", "creator", "owner"}:
        return True
    if status == "restricted":
        return getattr(member, "is_member", False) is True
    return False


async def _check_force_join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending_action: str | None = None,
) -> bool:
    if _is_admin(update):
        return False
    config = _force_join_config()
    if not config["enabled"]:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return True
    if not config["valid"]:
        log.error("Force-join configuration invalid: %s", config["error"])
        record_growth_event("gate_shown", user_id, source="force_join_misconfigured")
        text = (
            "🔒 *Access is temporarily unavailable.*\n\n"
            "The join check is misconfigured. An admin has been notified. Please try again later."
        )
        if update.callback_query:
            await update.callback_query.answer("Access check unavailable", show_alert=True)
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.MARKDOWN
            )
        elif update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return True
    channels = config["channels"]
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(ch["id"], user_id)
            if not _member_has_access(member):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    if not not_joined:
        return False
    record_growth_event("gate_shown", user_id, source="force_join")
    if pending_action:
        context.user_data["pending_after_join"] = pending_action
    else:
        context.user_data.pop("pending_after_join", None)
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
    config = _force_join_config()
    if not config["enabled"] or not config["valid"]:
        record_growth_event("verify_failure", user_id, source="force_join_config")
        await q.answer("❌ Access check unavailable", show_alert=True)
        return
    channels = config["channels"]
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(ch["id"], user_id)
            if not _member_has_access(member):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    if not_joined:
        record_growth_event("verify_failure", user_id, source="force_join")
        names = ", ".join(ch["name"] for ch in not_joined)
        await q.answer(f"❌ Still not joined: {names}", show_alert=True)
        return
    record_growth_event("verify_success", user_id, source="force_join")
    await q.answer("✅ Verified! Welcome in… 😏")
    pending_action = context.user_data.pop("pending_after_join", None)
    if pending_action:
        if pending_action.startswith("video:"):
            video_id = int(pending_action.split(":", 1)[1])
            try:
                await q.message.delete()
            except Exception:
                pass
            await _deliver_with_quota(
                update, context, video_id, allow_redirect=False
            )
            return
        if pending_action == "random":
            limit = get_user_size_limit(user_id)
            row = db_random(limit)
            if row:
                try:
                    await q.message.delete()
                except Exception:
                    pass
                await _deliver_with_quota(
                    update, context, row["id"], allow_redirect=True
                )
                return
            await q.edit_message_text(
                "🥺 The library is empty. Try again after the next import.",
                reply_markup=main_menu(user_id),
            )
            return

    markup = main_menu(user_id)
    welcome_text = _welcome_message(user_id)
    await _send_welcome(update, context, welcome_text, markup)

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
    data = context.job.data if context.job else None
    if not isinstance(data, dict):
        log.warning("Could not auto-delete video: scheduled job data is missing")
        return
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
        log.info("Auto-deleted video message %s from chat %s", data["message_id"], data["chat_id"])
    except Exception as e:
        log.warning(
            "Could not auto-delete video message %s from chat %s: %s",
            data.get("message_id"), data.get("chat_id"), e,
        )


def _auto_delete_minutes() -> int | None:
    """Return the configured delay, or None when auto-delete is disabled."""
    if get_bot_setting("auto_delete_enabled", "1") != "1":
        return None
    try:
        return max(1, int(get_bot_setting("auto_delete_minutes", "30") or "30"))
    except (TypeError, ValueError):
        log.warning("Invalid auto_delete_minutes setting; falling back to 30")
        return 30


def _append_auto_delete_notice(caption: str, minutes: int) -> str:
    unit = "minute" if minutes == 1 else "minutes"
    return (
        f"{caption}\n\n"
        f"⚠️ *Save it before it disappears.* This video auto-deletes in *{minutes} {unit}*."
    )


def _message_id_from_send_result(sent_msg) -> int | None:
    """Normalize PTB Message, MessageId, nested wrappers, and raw integers."""
    value = sent_msg
    for _ in range(3):
        if isinstance(value, int):
            return value
        value = getattr(value, "message_id", None)
        if value is None:
            return None
    return value if isinstance(value, int) else None


def _schedule_auto_delete(context, chat_id: int, sent_msg, minutes: int) -> bool:
    msg_id = _message_id_from_send_result(sent_msg)
    if msg_id is None:
        log.warning("Auto-delete not scheduled: Telegram send returned no message ID")
        return False
    if not context.job_queue:
        log.error("Auto-delete not scheduled: PTB JobQueue is unavailable")
        return False
    context.job_queue.run_once(
        _auto_delete_message,
        when=minutes * 60,
        data={"chat_id": chat_id, "message_id": msg_id},
        name=f"autodel_{chat_id}_{msg_id}",
    )
    log.info("Scheduled auto-delete for message %s in %s minute(s)", msg_id, minutes)
    return True


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
                return await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=int(channel_id),
                    message_id=int(message_id),
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            if file_ref.startswith("channel_ref:"):
                # Stored by the Telethon scanner: channel_ref:channel_id:message_id
                _, channel_id, message_id = file_ref.split(":", 2)
                return await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=int(channel_id),
                    message_id=int(message_id),
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            return await bot.send_video(
                chat_id=chat_id,
                video=file_ref,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
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


def _local_video_caption(row, meta=None) -> str:
    """Render a truthful local fallback without placeholder titles or fake duration."""
    data = dict(row)
    metadata = dict(meta) if meta else {}
    raw_title = data.get("title") or ""
    if raw_title and not is_placeholder_title(raw_title):
        title = clean_title_for_display(raw_title)
    else:
        performer = (
            metadata.get("corrected_performer") or metadata.get("stashdb_performer") or ""
        ).strip(", ")
        category = (data.get("category") or "").strip()
        source = (data.get("source_channel") or "").strip()
        if performer and category:
            title = f"{performer} — {category}"
        else:
            title = performer or category or source or "Library selection"
    details = []
    duration = int(data.get("duration") or 0)
    if duration > 0:
        details.append(f"⏱ {format_duration(duration)}")
    details.append(f"👁 {int(data.get('view_count') or 0) + 1}")
    return f"📁 *{title}*\n\n{' | '.join(details)}"


async def _notify_referral_activation(context, user_id: int) -> None:
    """Credit and notify a referral only after the first delivered video."""
    try:
        bonus = max(1, int(get_bot_setting("referral_bonus", "10") or "10"))
        result = activate_pending_referral(user_id, bonus)
    except Exception:
        log.exception("Referral activation failed for user %s", user_id)
        return
    if not result:
        return
    referrer_id = result["referrer_id"]
    refs = result["count"]
    record_growth_event(
        "referral_activation", user_id, source="referral", payload=str(referrer_id)
    )
    if result["premium_awarded"]:
        text = (
            f"🎉 *Premium unlocked!*\n\n"
            f"You reached *{refs} activated referrals* and earned *30 days Premium*. 👑\n"
            f"Your next Premium reward is at *{refs + 10}*."
        )
        log_text = f"Referral activated: `{user_id}` → `{referrer_id}`; {refs} total, Premium awarded"
    else:
        next_milestone = ((refs // 10) + 1) * 10
        needed = next_milestone - refs
        text = (
            f"🎁 *Referral activated!*\n\n"
            f"You earned *+{bonus} bonus watches*.\n"
            f"Only *{needed} more* for 30 days Premium."
        )
        log_text = f"Referral activated: `{user_id}` → `{referrer_id}` (+{bonus})"
    await _log_event(context, log_text)
    try:
        await context.bot.send_message(
            chat_id=referrer_id, text=text, parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        log.debug("Could not notify referrer %s", referrer_id)


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
        return False

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
                    return False
                filename = row["title"] or ""

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
        caption_text = _local_video_caption(row, meta)

    is_admin = _is_admin(update)
    delete_mins = None if is_admin else _auto_delete_minutes()
    if delete_mins is not None:
        caption_text = _append_auto_delete_notice(caption_text or "", delete_mins)

    show_rename = bool(
        meta and meta.get("stashdb_confidence", 0) and meta["stashdb_confidence"] >= 0.9
    )
    bot_username = context.bot_data.get("bot_username")
    if not bot_username:
        try:
            bot_username = (await context.bot.get_me()).username
            context.bot_data["bot_username"] = bot_username
        except Exception as e:
            log.debug("Could not resolve bot username for video sharing: %s", e)

    markup = video_actions(
        video_id,
        show_rename=show_rename,
        feedback_enabled=_stash_available,
        show_similar=bool(row_tags or cast),
        performers=cast[:4],
        user_id=update.effective_user.id if update.effective_user else None,
        is_admin=is_admin,
        bot_username=bot_username,
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

    # Telegram has confirmed delivery. Everything below is best-effort
    # bookkeeping and must never turn a successful send into a quota refund.
    try:
        increment_view(video_id)
    except Exception:
        log.exception("Post-send view bookkeeping failed for video %s", video_id)

    if update.effective_user:
        user_id = update.effective_user.id
        streak = None
        try:
            first_delivery = record_growth_event(
                "first_delivery", user_id, source="bot", video_id=video_id, first_only=True
            )
            if not first_delivery:
                record_growth_event("delivery", user_id, source="bot", video_id=video_id)
            streak = bump_streak(user_id)
            mark_welcome_conversion(user_id)
        except Exception:
            log.exception("Post-send user bookkeeping failed for user %s", user_id)
        try:
            await _notify_referral_activation(context, user_id)
        except Exception:
            log.exception("Post-send referral notification failed for user %s", user_id)
        if streak and streak["new_record"] and streak["streak"] >= 3:
            try:
                new_record_msg = f"🔥 *{streak['streak']}-day streak!* New personal best.\n"
                if not is_user_premium(user_id):
                    new_record_msg += f"⚡ Streak active → *+{streak['bonus']}* bonus watches daily."
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=new_record_msg,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                log.debug("Did not send streak milestone message: %s", e)

    try:
        await _maybe_send_ad(update, context)
    except Exception:
        log.exception("Post-send ad delivery failed")

    if delete_mins is not None:
        try:
            _schedule_auto_delete(context, chat_id, sent_msg, delete_mins)
        except Exception:
            log.exception("Post-send auto-delete scheduling failed")

    if not row.get("file_size"):
        try:
            asyncio.create_task(_lazy_fill_size(video_id, row["file_id"]))
        except Exception:
            log.exception("Post-send lazy size task failed")
    return True


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


async def _send_quota_exhausted(update: Update, context) -> None:
    quota = get_user_quota(update.effective_user.id)
    text = (
        "🚫 *Your free watches are used up.*\n\n"
        f"📊 `{quota['used']}/{quota['limit']}` used\n"
        "🎁 Activate referrals for bonus watches, or unlock Premium."
    )
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎁 Invite & Earn", callback_data="refer"),
            InlineKeyboardButton("👑 Premium", callback_data="get_premium"),
        ],
        [InlineKeyboardButton("🔙 Menu", callback_data="menu")],
    ])
    if update.callback_query:
        try:
            await update.callback_query.answer(
                "Free watches used up", show_alert=True
            )
        except Exception:
            pass
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN,
    )


async def _deliver_with_quota(
    update: Update,
    context,
    video_id: int,
    *,
    allow_redirect: bool,
) -> bool:
    """Reserve atomically, deliver, and refund any failed reservation."""
    user_id = update.effective_user.id
    charged = not _is_admin(update) and not is_user_premium(user_id)
    if charged and not increment_quota(user_id):
        await _send_quota_exhausted(update, context)
        return False
    if getattr(update, "callback_query", None):
        try:
            await update.callback_query.answer("Loading…")
        except Exception:
            pass
    try:
        delivered = await _enrich_and_send(
            update, context, video_id, allow_redirect=allow_redirect
        )
        if not delivered and charged:
            refund_quota(user_id)
        return bool(delivered)
    except Exception:
        if charged:
            refund_quota(user_id)
        raise


# ── Command: /start ────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    start_payload = context.args[0] if context.args else None
    start_video_id, start_referrer_id = _parse_start_payload(start_payload)
    if user:
        is_new = not user_exists(user.id)
        ensure_user_exists(user.id)
        if is_new:
            record_growth_event(
                "start", user.id, source=_acquisition_source(start_payload),
            )
        else:
            record_growth_event("session", user.id, source="returning")
        if is_new:
            get_or_assign_welcome_variant(user.id)
            if start_referrer_id is not None:
                # Attribution is pending until the new user passes force-join
                # and receives a first video. Empty starts earn nothing.
                set_pending_referral(start_referrer_id, user.id)
            username = f"@{user.username}" if user.username else "No username"
            full_name = filter(None, [user.first_name, user.last_name])
            name_str = " ".join(full_name) or "Unknown"
            await _log_event(context, f"👤 *New User*\nID: `{user.id}`\nUsername: {username}\nName: {name_str}")
            
    if start_video_id is not None:
        if await _check_force_join(
            update, context, pending_action=f"video:{start_video_id}"
        ):
            return
        try:
            vid_id = start_video_id
            await _deliver_with_quota(
                update, context, vid_id, allow_redirect=False
            )
            return
        except Exception as e:
            log.error("Error handling video deep link: %s", e, exc_info=True)

    if await _check_force_join(update, context):
        return
    markup = main_menu(user.id if user else None)
    welcome_text = _welcome_message(user.id if user else None)
    
    if update.callback_query:
        await update.callback_query.answer()
    await _send_welcome(update, context, welcome_text, markup)


# ── Command: /random ───────────────────────────────────────────────────────


async def random_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _check_force_join(update, context, pending_action="random"):
        return

    limit = get_user_size_limit(update.effective_user.id) if update.effective_user else None
    row = db_random(limit)
    if not row:
        text = "🥺 The library is empty. Try again after the next import."
        if update.callback_query:
            await update.callback_query.answer()
            await _replace_with_text(
                update, context, text, main_menu(update.effective_user.id)
            )
        else:
            await update.message.reply_text(
                text, reply_markup=main_menu(update.effective_user.id)
            )
        return

    log.info("random_video: delivering video_id=%s", row["id"])
    await _deliver_with_quota(
        update, context, row["id"], allow_redirect=True
    )


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
    context.user_data["search_query"] = query
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
                    "⬅️", callback_data=f"searchpage_{page - 1}"
                )
            )
        if (page + 1) * per_page < total:
            nav.append(
                InlineKeyboardButton(
                    "➡️", callback_data=f"searchpage_{page + 1}"
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
    page = int((q.data or "searchpage_0").split("_", 1)[1])
    query = context.user_data.get("search_query")
    if not query:
        await _replace_with_text(
            update, context, "⌛ Search expired. Start a new search.", back()
        )
        return
    await _show_search(update, context, query=query, page=page)


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
        experiment = get_welcome_experiment_stats()
        growth = get_growth_metrics(30)
        a = experiment["A"]
        b = experiment["B"]
        a_rate = (a["converted"] / a["exposed"] * 100) if a["exposed"] else 0.0
        b_rate = (b["converted"] / b["exposed"] * 100) if b["exposed"] else 0.0
        retention = growth["retention"]
        d1_rate = retention["d1"] / retention["d1_eligible"] * 100 if retention["d1_eligible"] else 0
        d7_rate = retention["d7"] / retention["d7_eligible"] * 100 if retention["d7_eligible"] else 0
        funnel = growth["funnel"]
        starts = funnel.get("start", {}).get("users", 0)
        gates = funnel.get("gate_shown", {}).get("users", 0)
        verified = funnel.get("verify_success", {}).get("users", 0)
        failed = funnel.get("verify_failure", {}).get("events", 0)
        first = funnel.get("first_delivery", {}).get("users", 0)
        sources = ", ".join(f"{s['source']} {s['users']}" for s in growth["sources"][:3]) or "none"
        text = (
            f"📊 *NoDick Stats*\n\n"
            f"👥 Users: {users:,}\n"
            f"📹 Videos: {total:,}\n"
            f"👁 Views: {views:,}\n"
            f"📁 Categories: {cats:,}\n\n"
            f"📈 *Growth — 30 days*\n"
            f"Starts: {starts} · Gate: {gates} · Verified: {verified} ({failed} failed)\n"
            f"First deliveries: {first}\n"
            f"DAU / WAU: {growth['dau']} / {growth['wau']}\n"
            f"D1: {retention['d1']}/{retention['d1_eligible']} ({d1_rate:.1f}%) · "
            f"D7: {retention['d7']}/{retention['d7_eligible']} ({d7_rate:.1f}%)\n"
            f"Sources: {sources}\n\n"
            f"🧪 *Welcome A/B — first watch*\n"
            f"A: {a['converted']}/{a['exposed']} ({a_rate:.1f}%)\n"
            f"B: {b['converted']}/{b['exposed']} ({b_rate:.1f}%)"
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
        category_tokens = {str(i): cat["category"] for i, cat in enumerate(cats)}
        context.user_data["category_tokens"] = category_tokens
        buttons = [
            InlineKeyboardButton(
                f"{cat['category']} ({cat['count']})",
                callback_data=f"cat_{i}",
            )
            for i, cat in enumerate(cats)
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
    data = q.data or ""
    token = ""
    if data.startswith("cat_"):
        token = data.split("_", 1)[1]
        page = 0
    elif data.startswith("catpage_"):
        _, payload = data.split("_", 1)
        token, page = payload.rsplit("|", 1)
        page = int(page)
    else:
        return

    category = context.user_data.get("category_tokens", {}).get(token)
    if not category:
        await _replace_with_text(
            update, context, "⌛ Category menu expired. Open Categories again.", back()
        )
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
                "⬅️", callback_data=f"catpage_{token}|{page - 1}"
            )
        )
    if (page + 1) * per_page < total:
        nav.append(
            InlineKeyboardButton(
                "➡️", callback_data=f"catpage_{token}|{page + 1}"
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
    if q:
        await q.answer()

    page = 0
    if q and (q.data or "").startswith("favpage_"):
        page = int((q.data or "").split("_", 1)[1])

    per_page = 10
    rows, total = get_favorites(
        user_id, page=page, per_page=per_page,
        max_size_mb=get_user_size_limit(user_id),
    )
    if not rows:
        text = "⭐ No saved videos yet. Tap ♡ Save on any video."
        if q:
            await _replace_with_text(update, context, text, main_menu(user_id))
        else:
            await update.message.reply_text(text, reply_markup=main_menu(user_id))
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

    text = f"⭐ Saved — {len(rows)} of {total}"
    markup = InlineKeyboardMarkup(buttons)
    if q:
        await _replace_with_text(update, context, text, markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def add_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⭐ Toggled!")
    video_id = int(q.data.split("_", 1)[1])
    from nodick.db import toggle_favorite

    toggle_favorite(update.effective_user.id, video_id)


# ── Play (callback from inline buttons) ────────────────────────────────────


async def play_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    video_id = int(q.data.split("_", 1)[1])
    if await _check_force_join(
        update, context, pending_action=f"video:{video_id}"
    ):
        return
    await _deliver_with_quota(
        update, context, video_id, allow_redirect=False
    )


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
        await q.edit_message_text("🚀 Triggering post to all channels...", reply_markup=back())
        successes = await send_auto_post(context.bot, force=True)
        if successes:
            await context.bot.send_message(
                update.effective_chat.id,
                f"✅ Auto-Posted to {successes} channel(s)."
            )
        else:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Failed or cooldown active. Check bot is admin in all channels."
            )
        return

    context.user_data["waiting_for_setting"] = action
    
    prompts = {
        "autodelete_timer": "Send me the new *Auto-Delete timer* in minutes (e.g. `30`):",
        "payment": "Send me the new *Payment Info* text (supports UPI, links, etc.):",
        "paymentqr": "Send me a *Photo* of your QR code:\n\n_(Send `clear` to remove an existing QR)_",
        "refbonus": "Send me the new *Referral Bonus* amount (e.g. `5`):",
        "logschannel": "Send me the new *Logs Channel ID* (e.g. `-1001234567890`):",
        "forcejoin": "Send me the *Channel/Group IDs* separated by spaces (e.g. `-100123 -100456`):\n\n_(Send `clear` to disable Force Join)_",
        "autopost_cooldown": "Send me the new *Auto-Post Cooldown* in hours (e.g. `6`):\n\nHow long to wait between auto-posts.",
        "autopost_channels": "Send me the *Channel IDs* to auto-post to, separated by spaces (e.g. `-100123 -100456`):\n\n_(Send `clear` to use the default channel)_",
        "coadmins": "Send me the *Admin IDs* separated by spaces (e.g. `12345 67890`):\n\n_(Send `clear` to remove all extra admins)_",
        "grantpremium": "Send me the *User IDs* you want to grant 30-Day Premium to, separated by spaces (e.g. `12345 67890`):",
        "revokepremium": "Send me the *User IDs* you want to revoke Premium from, separated by spaces:",
        "grantadmin": "Send me the *User IDs* to grant Admin rights to, separated by spaces:",
        "revokeadmin": "Send me the *Admin IDs* to revoke Admin rights from, separated by spaces:",
        "welcomegif": "Send a *Photo, GIF, or MP4* for the Welcome Banner, or a direct media URL.\n\n_(Send `clear` to disable; /cancel to abort)_",
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
            f"Resume ID: {row.get('next_message_id') or '-'}\n"
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
    """Fire up the MessageIDImporter with persisted status and checkpoint."""
    user_id = update.effective_user.id
    job_id = create_import_job(str(channel_id), user_id)
    message = update.message or update.callback_query.message
    status_msg = await message.reply_text(
        f"🛰 Scan #{job_id}: `{channel_id}` from message `{start_id}`"
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
            job_id=job_id,
            progress=progress,
        )

        next_id = int(stats.get("next_message_id") or 0)
        if stats.get("status") == "failed":
            if next_id > 0:
                set_bot_setting("scan_last_message_id", str(next_id))
            await status_msg.edit_text(
                f"❌ Scan #{job_id} failed.\n"
                f"Checked: {stats['total_checked']:,}\n"
                f"Resume ID: {next_id or start_id}\n\n"
                "Open Import Status for the recorded error."
            )
            return

        set_bot_setting("scan_last_message_id", "")
        await status_msg.edit_text(
            f"✅ Scan #{job_id} complete!\n"
            f"Checked: {stats['total_checked']:,}\n"
            f"Videos found: {stats['videos_found']}\n"
            f"Saved: {stats['saved']}\n"
            f"Skipped: {stats['skipped']}\n"
            "Forward the latest channel video when you want to scan new additions."
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
        f"🛰 Preparing scan of `{channel_id}` from `{message_id}`…",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _start_message_import(update, context, channel_id, message_id)


# ── Video upload handler ───────────────────────────────────────────────────


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not _is_admin(update):
        await msg.reply_text("🔒 Only approved admins can add videos to the library.")
        return
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


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any pending admin/user input state."""
    for key in (
        "waiting_for_search", "waiting_for_correction", "waiting_for_setting"
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "❌ Action cancelled.", reply_markup=main_menu(update.effective_user.id)
    )


async def _broadcast_worker(context, source_message, user_ids: list[int], admin_chat_id: int):
    """Copy one Telegram message safely to a snapshot of users."""
    lock = context.application.bot_data.setdefault("broadcast_lock", asyncio.Lock())
    if lock.locked():
        await context.bot.send_message(admin_chat_id, "⚠️ A broadcast is already running.")
        return
    async with lock:
        started = asyncio.get_running_loop().time()
        sent = failed = retried = 0
        status = await context.bot.send_message(
            admin_chat_id, f"📡 Broadcasting to {len(user_ids)} users…"
        )
        for index, uid in enumerate(user_ids, 1):
            delivered = False
            for attempt in range(3):
                try:
                    await context.bot.copy_message(
                        chat_id=uid,
                        from_chat_id=source_message.chat_id,
                        message_id=source_message.message_id,
                    )
                    sent += 1
                    delivered = True
                    break
                except RetryAfter as exc:
                    retried += 1
                    retry_after = exc.retry_after
                    delay = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after or 1)
                    await asyncio.sleep(min(delay, 30) + 0.25)
                except (TimedOut, NetworkError):
                    retried += 1
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                except (Forbidden, BadRequest) as exc:
                    log.debug("Broadcast rejected for user %s: %s", uid, type(exc).__name__)
                    break
                except Exception:
                    log.exception("Broadcast failed for user %s", uid)
                    break
            if not delivered:
                failed += 1
            await asyncio.sleep(0.05)
            if index % 25 == 0:
                try:
                    await status.edit_text(f"📡 Broadcasting… {index}/{len(user_ids)}")
                except Exception:
                    pass
        elapsed = int(asyncio.get_running_loop().time() - started)
        summary = (
            f"📡 Broadcast complete!\n✅ Sent: {sent}\n❌ Failed: {failed}"
            f"\n🔄 Retried: {retried}\n⏱ {elapsed}s"
        )
        try:
            await status.edit_text(summary)
        except Exception:
            await context.bot.send_message(admin_chat_id, summary)
        ch_id = get_bot_setting("logs_channel_id", "")
        if ch_id and str(ch_id) != str(admin_chat_id):
            try:
                await context.bot.send_message(int(ch_id), summary)
            except Exception:
                log.exception("Could not mirror broadcast summary to logs channel")


async def broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture any copyable message while the admin broadcast prompt is active."""
    if context.user_data.get("waiting_for_setting") != "broadcast":
        return
    if not _is_admin(update):
        raise ApplicationHandlerStop
    context.user_data.pop("waiting_for_setting", None)
    user_ids = list(dict.fromkeys(int(uid) for uid in get_all_user_ids()))
    if not user_ids:
        await update.message.reply_text("📡 No users to broadcast to yet.")
        raise ApplicationHandlerStop
    context.application.create_task(
        _broadcast_worker(context, update.message, user_ids, update.effective_chat.id)
    )
    await update.message.reply_text(f"📡 Broadcast queued for {len(user_ids)} users.")
    raise ApplicationHandlerStop


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
            # Broadcast input is intercepted before this text router so that
            # copy_message preserves entities and every supported media type.
            msg = "⚠️ Broadcast state expired. Tap Broadcast and send the message again."
            
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
            context.user_data["waiting_for_setting"] = setting
            return await welcome_media_input(update, context)
                
        elif setting == "refergif":
            if text.lower() == "clear":
                set_bot_setting("refer_gif", "")
                msg = "✅ Referral GIF disabled."
            else:
                set_bot_setting("refer_gif", text)
                msg = "✅ Referral GIF updated!"
        elif setting == "autopost_cooldown":
            try:
                n = int(text)
                if n < 0 or n > 168:
                    msg = "❌ Cooldown must be 0-168 hours (0 = no cooldown)."
                else:
                    set_bot_setting("auto_post_cooldown", str(n))
                    msg = f"✅ Auto-post cooldown set to {n} hours."
            except ValueError:
                msg = "❌ Must be a number in hours."
        elif setting == "autopost_channels":
            import json as _json
            if text.lower() == "clear":
                set_bot_setting("auto_post_channels", "")
                msg = "✅ Auto-post channels cleared (using default channel)."
            else:
                ids = text.split()
                channels = []
                failed = []
                for cid in ids:
                    try:
                        chat_id = int(cid)
                        chat = await context.bot.get_chat(chat_id)
                        channels.append({
                            "id": chat_id,
                            "name": chat.title or str(chat_id),
                            "url": chat.invite_link or "",
                        })
                    except Exception as e:
                        failed.append(f"`{cid}`: {str(e)}")
                if channels:
                    set_bot_setting("auto_post_channels", _json.dumps(channels))
                    msg = f"✅ Saved {len(channels)} auto-post channel(s): " + ", ".join(f"`{c['name']}`" for c in channels)
                else:
                    msg = "❌ No valid channels saved. Make sure the bot is a member/admin."
                if failed:
                    msg += "\n\n⚠️ Failed:\n" + "\n".join(failed)
        
        await update.message.reply_text(msg, reply_markup=settings_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return

    if not text:
        return

    # Check if waiting for search query
    if context.user_data.pop("waiting_for_search", False):
        await _show_search(update, context, query=text, page=0)
        return

    # Check if waiting for correction text
    correction_video_id = context.user_data.pop("waiting_for_correction", None)
    if correction_video_id is not None:
        await _handle_correction_text(
            update, context, text, int(correction_video_id)
        )
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    video_id: int,
):
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

async def force_join_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admins a redacted, live force-join configuration health report."""
    if not _is_admin(update):
        if update.callback_query:
            await update.callback_query.answer("❌ Admin only.", show_alert=True)
        return
    config = _force_join_config()
    lines = ["🩺 *Force Join Health*", ""]
    if not config["enabled"]:
        lines.append("⚪ Disabled — no channels configured.")
    elif not config["valid"]:
        lines.extend(("🔴 Enabled but invalid — access fails closed.", f"Reason: `{config['error']}`"))
    else:
        lines.extend(("🟡 Enabled and valid.", "\nBot access to each channel (getChatMember is reliable when the bot is an administrator):"))
        me = await context.bot.get_me()
        lines.append(f"Configured channels: *{len(config['channels'])}*")
        for channel in config["channels"]:
            try:
                member = await context.bot.get_chat_member(channel["id"], me.id)
                status = str(getattr(member, "status", "")).lower()
                if status in {"administrator", "creator", "owner"}:
                    marker, detail = "🟢", "bot is administrator"
                elif status == "member":
                    marker, detail = "🟡", "getChatMember needs bot admin rights to be reliable"
                else:
                    marker, detail = "🔴", "bot lacks membership"
            except Exception:
                marker, detail = "🔴", "unreachable or permission denied"
            lines.append(f"{marker} *{channel['name']}* — {detail}")
        lines.append("\nChannel identifiers are intentionally hidden here.")
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.answer()
        await _replace_with_text(update, context, text, settings_keyboard())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def forcejoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: manage forced join channels."""
    if not _is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args or context.args[0].lower() == "status":
        await force_join_health(update, context)
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
        validation = _force_join_config(json.dumps(channels))
        if not validation["valid"]:
            await update.message.reply_text(
                f"❌ Rejected — the resulting config would fail closed: {validation['error']}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
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
    await q.answer("Send the replacement title")
    video_id = int(q.data.split("_", 1)[1])
    meta = get_video_metadata(video_id)
    suggested = ""
    if meta and meta.get("stashdb_title"):
        suggested = f"{meta['stashdb_performer'] or ''} — {meta['stashdb_title']}".strip(" —")
    context.user_data["waiting_for_correction"] = video_id
    text = "📝 *Rename video*\n\n"
    if suggested:
        text += f"Suggested: `{suggested}`\n\n"
    text += "Send `Performer — Title`, or /cancel."
    await _replace_with_text(update, context, text, back())


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
    raw = update.message.text or ""
    _, _, payload = raw.partition(" ")
    if payload.strip():
        await update.message.reply_text(
            "Use ⚙️ Settings → 📢 Broadcast, then send or forward the exact message. "
            "That preserves formatting and media."
        )
        return
    context.user_data["waiting_for_setting"] = "broadcast"
    await update.message.reply_text(
        "📢 Send or forward the message to broadcast. Text, photos, videos, GIFs, "
        "documents, audio and voice notes are supported. Send /cancel to abort."
    )

async def my_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    from nodick.telegram.keyboards import account_keyboard
    await _replace_with_text(update, context, "👤 *My Account*", account_keyboard(user_id))

async def my_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
    user_id = update.effective_user.id
    quota = get_user_quota(user_id)
    ref_count = get_referral_count(user_id)
    referral_bonus_total = get_referral_bonus_total(user_id)
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
    text += (
        f"🎁 Referrals: {ref_count}\n"
        f"⚡ Referral bonus earned: +{referral_bonus_total}\n"
    )

    # Streak section — always shown (0 days is a valid starting state)
    streak = get_user_streak(user_id)
    if not premium:
        # Daily bonus tier for non-premium users
        tier_desc = {0: "No bonus", 2: "+2 bonus watches", 3: "+3 bonus watches"}.get(streak["bonus"], f"+{streak['bonus']} bonus watches")
    else:
        tier_desc = "Unlimited (Premium)"
    bar7 = streak_bar(streak["streak"], 7)
    text += (
        f"\n🔥 *Watch Streak*\n"
        f"{bar7} *{streak['streak']}* days"
        f"  |  Best: *{streak['best']}*\n"
        f"{tier_desc}\n"
    )

    from nodick.telegram.keyboards import account_keyboard
    markup = account_keyboard(user_id)
    if q:
        await _replace_with_text(update, context, text, markup)
    else:
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )

async def refer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a share-ready referral card for callbacks and /refer alike."""
    if update.callback_query:
        await update.callback_query.answer()

    user_id = update.effective_user.id
    ref_count = get_referral_count(user_id)
    bonus_total = get_referral_bonus_total(user_id)
    social = get_referral_social_proof(user_id)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    try:
        bonus = max(1, int(get_bot_setting("referral_bonus", "10") or "10"))
    except (TypeError, ValueError):
        bonus = 10

    milestone = 10
    tier_progress = ref_count % milestone
    next_milestone = ((ref_count // milestone) + 1) * milestone
    needed = next_milestone - ref_count
    filled = 10 if tier_progress == 0 and ref_count else tier_progress
    progress_bar = f"[{'▮' * filled}{'▯' * (milestone - filled)}]"
    share_text = (
        "I found an adult content bot with instant streaming, no ads, and premium videos. "
        f"Join through my link and we both get perks: {ref_link}"
    )
    activity_lines = []
    if social["yours_today"]:
        activity_lines.append(
            f"🔥 *{social['yours_today']} of your invites joined today.*"
        )
    elif social["global_today"]:
        activity_lines.append(
            f"🔥 *{social['global_today']} people joined through invites today.*"
        )
    if social["leader_total"] > ref_count:
        gap = social["leader_total"] - ref_count
        activity_lines.append(
            f"🏆 The leaderboard has *{social['leader_total']} referrals* at the top — you're *{gap} away*."
        )
    social_proof = "\n".join(activity_lines)
    if social_proof:
        social_proof += "\n\n"

    text = (
        "🎁 *Invite & Earn*\n\n"
        f"{social_proof}"
        f"Invite friends to this adult content bot. Each *new* user who joins and watches "
        f"their first video gives you *+{bonus} bonus watches*.\n\n"
        f"👑 *Premium milestone*\n"
        f"{progress_bar} *{tier_progress}/{milestone}* toward your next month of Premium\n"
        f"Only *{needed} more* referral{'s' if needed != 1 else ''} until *{next_milestone} total*.\n\n"
        f"👥 Successful invites: *{ref_count}*\n"
        f"⚡ Bonus watches earned: *+{bonus_total}*\n\n"
        f"🔗 *Your personal link*\n`{ref_link}`\n\n"
        "Tap *📤 Share Invite* to send a ready-made Telegram invite. "
        "Only first-time users count — no loopholes, sadly."
    )
    markup = referral_keyboard(ref_link, share_text)
    refer_gif = get_bot_setting("refer_gif", "")

    if refer_gif:
        if update.callback_query:
            try:
                await update.callback_query.message.delete()
            except Exception:
                pass
        try:
            return await context.bot.send_animation(
                chat_id=update.effective_chat.id,
                animation=refer_gif,
                caption=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            log.debug("Referral GIF failed; falling back to text", exc_info=True)

    if update.callback_query:
        return await _replace_with_text(update, context, text, markup)
    return await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

async def get_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
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
        if q:
            try:
                await q.message.delete()
            except Exception:
                pass
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_id,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back()
        )
    elif q:
        await _replace_with_text(update, context, text, back())
    else:
        await update.message.reply_text(
            text, reply_markup=back(), parse_mode=ParseMode.MARKDOWN
        )

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
            from nodick.services.poster import send_auto_post
            await send_auto_post(context.bot)

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
    app.add_handler(CommandHandler("cancel", cancel_command))
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
    app.add_handler(CallbackQueryHandler(force_join_health, pattern="^forcejoin_health$"))
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
    # Pending broadcasts must claim all copyable messages before forwarded
    # channel detection or upload indexing consumes them.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, welcome_media_input), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, broadcast_input), group=-1)
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
    from nodick.web import start_web_server

    if _sys.version_info >= (3, 10):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
    _port = int(_os.getenv("PORT", "8080"))
    server = None
    try:
        # Build the application (initializes the DB) BEFORE marking HTTP
        # health ready, so Render traffic never hits an uninitialized app.
        application = build_application()
        # Render health endpoint only — the bot is pure Telegram again.
        server = start_web_server(_port)
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

        application.run_polling()
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
                log.info("Health server shut down")
            except Exception:
                log.exception("Web server shutdown failed")
