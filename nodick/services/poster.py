import io
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from nodick.config import settings
from nodick.db import _fetchone, get_bot_setting, get_video
from nodick.metadata.stash import query_api

try:
    from PIL import Image, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
import requests

log = logging.getLogger(__name__)


# ── Image helpers ────────────────────────────────────────────────────────────

def fetch_scene_image(scene_id: str) -> Optional[bytes]:
    if not settings.stash_configured:
        return None
    query = """
    query FindScene($id: ID!) {
      findScene(id: $id) {
        images { url }
      }
    }
    """
    try:
        data = query_api(
            settings.stashdb_graphql_url, settings.stashdb_api_key,
            query, {"id": scene_id}
        )
        scene = (data or {}).get("findScene") or {}
        images = scene.get("images") or []
        if images:
            resp = requests.get(images[0]["url"], timeout=10)
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        log.error("Failed to fetch scene image for %s: %s", scene_id, e)
    return None


def create_blurred_thumbnail(image_bytes: bytes) -> bytes:
    """Heavy Gaussian blur + darken for SFW channel safety."""
    if not HAS_PIL:
        return image_bytes
    try:
        from PIL import Image, ImageFilter
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            blurred = img.filter(ImageFilter.GaussianBlur(50))
            darkened = Image.eval(blurred, lambda x: int(x * 0.7))
            out = io.BytesIO()
            darkened.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except Exception as e:
        log.error("Failed to blur image: %s", e)
        return image_bytes


def create_fallback_image() -> bytes:
    """Plain dark placeholder when no image can be generated."""
    if not HAS_PIL:
        return b""
    try:
        from PIL import Image
        img = Image.new("RGB", (800, 450), color=(20, 20, 24))
        out = io.BytesIO()
        img.save(out, format="JPEG")
        return out.getvalue()
    except Exception:
        return b""


async def fetch_telegram_thumb(video: dict) -> Optional[bytes]:
    """Pull the video's own Telegram thumbnail via Telethon, if available.

    Uses ``source_chat_id`` + ``source_message_id`` (set by the importer) to
    locate the original message, then extracts a document thumbnail and
    downloads it. Returns raw JPEG bytes, or None if unavailable.

    This is async because Telethon is async; it uses the cached bot client so
    it doesn't reconnect per post.
    """
    chat_id = video.get("source_chat_id")
    msg_id = video.get("source_message_id")
    if not chat_id or not msg_id:
        return None
    try:
        from nodick.services.message_importer import _get_telethon_client
        client = await _get_telethon_client()
        msgs = await client.get_messages(chat_id, ids=int(msg_id))
        msg = msgs[0] if msgs else None
        if not msg or not getattr(msg, "media", None):
            return None
        # Prefer the document/video thumbnail
        media = getattr(msg, "document", None) or getattr(msg, "video", None) or None
        thumb = None
        if media is not None and getattr(media, "thumbs", None):
            thumb = media.thumbs[0]  # often a thumbnail photo/size
        if thumb is None:
            return None
        import io as _io
        from telethon.tl.types import PhotoSize  # noqa: F401
        out = _io.BytesIO()
        await client.download_media(thumb, file=out)
        data = out.getvalue()
        return data if data else None
    except Exception as e:
        log.info("Telegram thumb unavailable for video %s: %s", video.get("id"), e)
        return None


# ── Channel / cooldown state (bot_settings-backed, both backends) ────────────

def _get_channels() -> list[dict]:
    """Multi-channel list; falls back to the legacy single-channel key."""
    raw = get_bot_setting("auto_post_channels", "")
    if raw:
        try:
            channels = json.loads(raw)
            if channels:
                return channels
        except Exception:
            pass
    legacy = get_bot_setting("auto_post_channel", "")
    if legacy:
        return [{"id": legacy, "name": f"Channel {legacy}"}]
    return []


def _mark_posted(video_id: int) -> None:
    """Record this video as posted + refresh the last-posted timestamp."""
    from nodick.db import set_bot_setting
    try:
        posted = json.loads(get_bot_setting("auto_post_posted_ids", "[]") or "[]")
    except Exception:
        posted = []
    # Dedup + keep most recent first, cap growth
    posted = [v for v in posted if v != video_id]
    posted.append(video_id)
    set_bot_setting("auto_post_posted_ids", json.dumps(posted[-400:]))
    set_bot_setting("auto_post_last", datetime.now().isoformat())


def _cooldown_active() -> bool:
    """True if the cooldown window hasn't elapsed since the last post."""
    last_str = get_bot_setting("auto_post_last", "")
    if not last_str:
        return False
    cooldown_hours = int(get_bot_setting("auto_post_cooldown", "6") or "6")
    try:
        last = datetime.fromisoformat(last_str)
    except Exception:
        return False
    return datetime.now() - last < timedelta(hours=cooldown_hours)


# ── Video selection (random from existing stash, repeats avoided) ────────────
# Query uses the existing rows regardless of posted history, but we keep trying
# until we land on an un-posted video. Bounded retries keep it cheap; if the
# whole stash has been posted, we accept a repeat (the cooldown caps rate).

SELECT_SQL = """
    SELECT v.id, v.title, v.category, v.tags,
           m.stashdb_scene_id, m.stashdb_title,
           m.stashdb_performer, m.stashdb_studio,
           v.source_chat_id, v.source_message_id
    FROM videos v
    JOIN video_metadata m ON v.id = m.video_id
    WHERE m.stashdb_scene_id IS NOT NULL
    ORDER BY RANDOM() LIMIT 1
"""


def _select_one() -> Optional[dict]:
    row = _fetchone(SELECT_SQL)
    if not row:
        return None
    return dict(row)


def _select_video() -> Optional[dict]:
    """Random stash pick, retrying to avoid re-posting the same recent video."""
    try:
        posted = set(json.loads(get_bot_setting("auto_post_posted_ids", "[]") or "[]"))
    except Exception:
        posted = set()

    attempts = 0
    while attempts < 15:
        v = _select_one()
        if not v:
            return None
        if v["id"] not in posted or len(posted) == 0:
            return v
        attempts += 1
    # Whole (recent) stash already posted → accept whatever we last pulled
    return _select_one()


# ── Main poster ──────────────────────────────────────────────────────────────

async def send_auto_post(bot: Bot, channel_id: str | None = None, force: bool = False) -> int:
    """Post one blurred teaser to all configured channels.

    If ``channel_id`` is passed, post only to that channel (manual trigger).
    ``force=True`` bypasses the cooldown (manual \"Post Now\").
    Returns number of successful posts (0 on cooldown/no channels/no videos).
    """
    if _cooldown_active() and not force:
        log.info("Auto-post skipped: cooldown active")
        return 0

    channels = _get_channels()
    if channel_id:
        channels = [c for c in channels if c["id"] == channel_id]
    if not channels:
        log.warning("No autopost channels configured")
        return 0

    video = _select_video()
    if not video:
        log.warning("No videos available for autopost")
        return 0

    vid_id = video["id"]
    scene_id = video.get("stashdb_scene_id")
    title = video.get("stashdb_title") or video.get("title") or "Exclusive Video"
    performer = video.get("stashdb_performer") or "Unknown"
    category = video.get("category") or "Vault"

    # Build image, best chain first: StashDB scene art → the video's own
    # Telegram thumbnail → plain placeholder. All blurred for SFW safety.
    img_data = fetch_scene_image(scene_id) if scene_id else None
    if not img_data:
        img_data = await fetch_telegram_thumb(video)
    blurred = create_blurred_thumbnail(img_data) if img_data else create_fallback_image()
    if not blurred:
        log.error("Could not generate any image for autopost")
        return 0

    caption = (
        f"🎬 **{title}**\n\n"
        f"👤 {performer}\n"
        f"🏷 #{category.replace(' ', '')}\n\n"
        f"❤️ *[ Content Hidden ]*\n\n"
        f"👇 *Tap below to instantly unlock the full uncensored video.*"
    )

    bot_info = await bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=vid_{vid_id}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Watch in Bot", url=deep_link)]
    ])

    successes = 0
    for ch in channels:
        try:
            await bot.send_photo(
                chat_id=ch["id"],
                photo=blurred,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=markup,
            )
            successes += 1
            log.info("Posted autopost to %s (%s)", ch["id"], ch.get("name", ""))
        except Exception as e:
            log.error("Failed to post to channel %s: %s", ch["id"], e)

    if successes:
        _mark_posted(vid_id)

    return successes