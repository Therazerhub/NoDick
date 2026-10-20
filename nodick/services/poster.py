import asyncio
import io
import html
import re
from urllib.parse import quote
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from nodick.config import settings
from nodick.db import _fetchone, _using_pg, get_bot_setting, get_video
from nodick.metadata.stash import query_api

try:
    import PIL  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
import requests

log = logging.getLogger(__name__)


# ── Image helpers ────────────────────────────────────────────────────────────

def fetch_scene_image(scene_id: str) -> Optional[bytes]:
    if not settings.stash_configured:
        log.warning("poster: STASHDB_API_KEY not configured — skipping StashDB art")
        return None
    query = """
    query FindScene($id: ID!) {
      findScene(id: $id) {
        images { url width height }
      }
    }
    """
    try:
        payload = query_api(
            settings.stashdb_graphql_url, settings.stashdb_api_key,
            query, {"id": scene_id}
        )
        # query_api returns the full GraphQL envelope ({"data": {...}}).
        # Reading findScene from the top level silently discarded every valid
        # response and forced the poster into its fallback path.
        scene = ((payload or {}).get("data") or {}).get("findScene") or {}
        images = scene.get("images") or []
        if not images:
            log.warning("poster: scene %s has no StashDB images", scene_id)
            return None
        # Pick the LARGEST image (first entry can be a tiny thumbnail)
        best = max(
            images,
            key=lambda im: (im.get("width") or 0) * (im.get("height") or 0),
        )
        url = best.get("url")
        if not url:
            return None
        # CDNs (and stashdb itself) reject requests without a UA
        resp = requests.get(
            url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NoDickPoster/1.0)"},
        )
        if resp.status_code == 200 and resp.content:
            log.info("poster: StashDB image OK (%s bytes, %sx%s)",
                     len(resp.content), best.get("width"), best.get("height"))
            return resp.content
        log.warning("poster: StashDB image HTTP %s for scene %s", resp.status_code, scene_id)
    except Exception as e:
        log.error("Failed to fetch scene image for %s: %s", scene_id, e)
    return None


def create_blurred_thumbnail(image_bytes: bytes) -> bytes:
    """Create a consistent, dark SFW teaser without flattening small thumbs.

    Every source is first cover-fitted to one canvas. That matters: applying a
    25px blur directly to a 200px Telegram thumbnail destroys all contrast and
    produces the gray rectangle users were seeing. Upscaling first keeps the
    blur visually intentional while still hiding explicit detail.
    """
    if not HAS_PIL:
        return image_bytes
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
        with Image.open(io.BytesIO(image_bytes)) as source:
            source = ImageOps.exif_transpose(source).convert("RGB")
            original_size = source.size
            canvas = ImageOps.fit(
                source,
                (960, 540),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

        blurred = canvas.filter(ImageFilter.GaussianBlur(22))
        darkened = ImageEnhance.Brightness(blurred).enhance(0.68)
        # A restrained violet grade keeps the teaser deliberate rather than
        # looking like Telegram failed to load a monochrome placeholder.
        graded = Image.blend(darkened, Image.new("RGB", darkened.size, (15, 6, 24)), 0.10)

        out = io.BytesIO()
        graded.save(out, format="JPEG", quality=86, optimize=True)
        result = out.getvalue()
        contrast = sum(ImageStat.Stat(graded).stddev) / 3
        log.info(
            "poster: blurred source %sx%s → 960x540 radius=22 contrast=%.1f (%s bytes)",
            original_size[0], original_size[1], contrast, len(result),
        )
        return result
    except Exception as e:
        log.error("Failed to blur image: %s", e)
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
        log.warning(
            "poster: video %s has no source_chat_id/source_message_id for Telegram thumbnail",
            video.get("id"),
        )
        return None
    try:
        from nodick.services.message_importer import _get_telethon_client
        client = await _get_telethon_client()
        msgs = await client.get_messages(chat_id, ids=int(msg_id))
        msg = msgs[0] if msgs else None
        if not msg or not getattr(msg, "media", None):
            log.warning("poster: source message %s has no media", msg_id)
            return None

        # Ask Telethon to resolve and download the largest available preview
        # from the original message. Passing media.thumbs[0] directly often
        # selects a tiny stripped thumbnail (or fails to download at all).
        out = io.BytesIO()
        await client.download_media(msg, file=out, thumb=-1)
        data = out.getvalue()
        if data:
            log.info("poster: Telegram thumbnail OK (%s bytes) for video %s", len(data), video.get("id"))
            return data
        log.warning("poster: Telegram source message %s returned an empty thumbnail", msg_id)
        return None
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
    """Pick an unposted candidate with one bounded database query."""
    try:
        posted = [
            int(value)
            for value in json.loads(
                get_bot_setting("auto_post_posted_ids", "[]") or "[]"
            )[-400:]
        ]
    except Exception:
        posted = []

    if not posted:
        return _select_one()
    ph = "%s" if _using_pg else "?"
    placeholders = ",".join(ph for _ in posted)
    sql = SELECT_SQL.replace(
        "ORDER BY RANDOM() LIMIT 1",
        f"AND v.id NOT IN ({placeholders}) ORDER BY RANDOM() LIMIT 1",
    )
    row = _fetchone(sql, tuple(posted))
    if row:
        return dict(row)
    # The recent-history window covers the whole enriched library.
    return _select_one()


async def _fetch_teaser_source(video: dict) -> tuple[Optional[bytes], str]:
    """Return the first real image source and its diagnostic label."""
    scene_id = video.get("stashdb_scene_id")
    if scene_id:
        image = await asyncio.to_thread(fetch_scene_image, scene_id)
        if image:
            return image, "stashdb"
    image = await fetch_telegram_thumb(video)
    if image:
        return image, "telegram"
    return None, "none"


# ── Draft 1 caption ──────────────────────────────────────────────────────────

def _caption_field(value, limit: int) -> str:
    """Omit import placeholders; bound plain text before HTML escaping.

    Limits are UTF-16 code units, including the truncation ellipsis. Splitting
    by Python characters avoids cutting a non-BMP character in half.
    """
    text = " ".join(str(value or "").split())
    if text.casefold() in {"", "unknown", "none", "null", "n/a", "na", "not set", "-", "?", "exclusive video", "vault"}:
        return ""
    if re.fullmatch(r"(?:video|file|untitled)(?:[\s_\-]*\d+)?|\d+", text, re.I):
        return ""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    result = []
    remaining = limit - 1
    for char in text:
        size = len(char.encode("utf-16-le")) // 2
        if size > remaining:
            break
        result.append(char)
        remaining -= size
    return "".join(result).rstrip() + "…"


def build_auto_post_caption(video: dict, bot_username: str) -> str:
    """Render only persisted metadata, with a bounded, balanced HTML caption.

    There is no reliable description column in video_metadata, so Draft 1's
    optional description is intentionally omitted (no generated synopsis).
    Field budgets plus labels/footer stay below 1024 parsed UTF-16 units.
    """
    title = _caption_field(video.get("stashdb_title"), 280) or _caption_field(video.get("title"), 280)
    performer = _caption_field(video.get("stashdb_performer"), 180)
    studio = _caption_field(video.get("stashdb_studio"), 120)
    category = _caption_field(video.get("category"), 96)
    username = _caption_field(bot_username, 64)
    sections = [f"<b>{html.escape(title)}</b>"] if title else []
    lines = []
    for label, value in (("Featuring", performer), ("Studio", studio), ("Category", category)):
        if value:
            lines.append(f"➜ {label}: <b>{html.escape(value)}</b>")
    if lines:
        sections.append("\n".join(lines))
    # Punctuation is removed, word separators become underscores; don't
    # manufacture a hashtag when the category has no usable characters.
    hashtag = re.sub(r"[^\w\s-]", "", category, flags=re.UNICODE)
    hashtag = re.sub(r"[\s-]+", "_", hashtag).strip("_")
    if hashtag and hashtag[0].isdigit():
        hashtag = "category_" + hashtag
    footer = ([f"#{html.escape(hashtag)}"] if hashtag else [])
    footer.append(f"Open the complete video in @{html.escape(username)}.")
    sections.append("<blockquote>" + "\n".join(footer) + "</blockquote>")
    return "\n\n".join(sections)


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

    # Never publish a placeholder: try several candidates until one has a real
    # StashDB image or Telegram thumbnail. A skipped post is better than the
    # feature advertising itself with a gray rectangle.
    video = None
    blurred = b""
    source = "none"
    tried_ids: set[int] = set()
    for _ in range(6):
        candidate = _select_video()
        if not candidate:
            break
        candidate_id = int(candidate["id"])
        if candidate_id in tried_ids:
            continue
        tried_ids.add(candidate_id)

        img_data, source = await _fetch_teaser_source(candidate)
        if not img_data:
            log.warning("poster: video %s has no usable image source; trying another", candidate_id)
            continue
        from nodick.services.thumbnail_safety import create_censored_thumbnail
        blurred = await create_censored_thumbnail(img_data)
        if not blurred:
            log.warning("poster: video %s image could not be rendered; trying another", candidate_id)
            continue
        video = candidate
        break

    if not video or not blurred:
        log.error("Auto-post aborted: no real thumbnail found after %s candidate(s)", len(tried_ids))
        return 0

    vid_id = video["id"]
    log.info("poster: using %s thumbnail for video %s", source, vid_id)

    bot_info = await bot.get_me()
    if not bot_info.username:
        log.error("Auto-post aborted: bot has no username for deep links")
        return 0
    caption = build_auto_post_caption(video, bot_info.username)
    bot_url = f"https://t.me/{quote(bot_info.username, safe='')}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Premium", url=f"{bot_url}?start=premium"),
         InlineKeyboardButton("How to Watch", url=f"{bot_url}?start=how_to_watch")],
        [InlineKeyboardButton("Open Video", url=f"{bot_url}?start=vid_{vid_id}")],
    ])

    successes = 0
    for ch in channels:
        try:
            await bot.send_photo(
                chat_id=ch["id"],
                photo=blurred,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
            successes += 1
            log.info("Posted autopost to %s (%s)", ch["id"], ch.get("name", ""))
        except Exception as e:
            log.error("Failed to post to channel %s: %s", ch["id"], e)

    if successes:
        _mark_posted(vid_id)

    return successes