"""MessageID Importer — Silent Telethon batch-fetch scanner.

Uses Telethon with BOT TOKEN (no user session) to batch-fetch messages by ID
list via MTProto. This is exactly what VJ bot does — read channel history
silently without forwarding or copying anything.

Stores references as channel_ref:channel_id:message_id for Bot API playback.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Awaitable, Optional
from collections import defaultdict

from telethon import TelegramClient

from nodick.config import settings
from nodick.db import upsert_video, update_import_job
from nodick.utils import (
    extract_category_from_title,
    title_from_filename_or_caption,
)

log = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], Awaitable[None]]

# Cache Telethon bot client across scans (avoids reconnecting every time)
_telethon_bot_client: Optional[TelegramClient] = None
_telethon_lock = asyncio.Lock()


async def _get_telethon_client() -> TelegramClient:
    """Get or create a Telethon client authenticated with bot token."""
    global _telethon_bot_client
    if _telethon_bot_client is not None and _telethon_bot_client.is_connected():
        return _telethon_bot_client

    async with _telethon_lock:
        if _telethon_bot_client is not None and _telethon_bot_client.is_connected():
            return _telethon_bot_client

        client = TelegramClient(
            "nodick_scan_bot",
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.start(bot_token=settings.bot_token)
        _telethon_bot_client = client
        log.info("Telethon bot client connected")
        return client


def _is_video(msg) -> bool:
    """Check if a Telethon message contains a video."""
    if not msg or not msg.media:
        return False
    if hasattr(msg, "video") and msg.video:
        return True
    if hasattr(msg, "document") and msg.document:
        mime = getattr(msg.document, "mime_type", "") or ""
        return mime.startswith("video/")
    return False


async def _batch_fetch(
    client: TelegramClient,
    channel_id: int,
    ids: list[int],
) -> list:
    """Fetch a batch of message IDs. Non-existent IDs are simply omitted."""
    try:
        return await client.get_messages(channel_id, ids=ids)
    except Exception as e:
        log.warning("Batch fetch error for ids %d-%d: %s", ids[0], ids[-1], e)
        return []


class MessageIDImporter:
    """Import videos from a channel using Telethon bot-token batch-fetch.

    VJ bot style: uses MTProto (via Telethon) with just a bot token to silently
    read channel history. No user session, no forwarding, no visible messages.

    Batches 100 IDs per call. Stores channel_ref references for Bot API playback.
    """

    def __init__(self, bot=None, staging_chat_id: int = 0):
        # bot and staging_chat_id kept for backward compat — not used
        self._last_copied_id = 0
        self._running = False

    async def import_channel(
        self,
        channel_id: int,
        start_message_id: int,
        *,
        job_id: Optional[int] = None,
        progress: Optional[ProgressCallback] = None,
        dry_run: bool = False,
        min_message_id: int = 1,
    ) -> dict:
        """Silently scan channel messages by ID — no forwarding.

        Args:
            channel_id: Source channel ID (negative integer).
            start_message_id: Highest message ID to start from.
            job_id: DB import job ID for tracking.
            progress: Async callback for progress updates.
            dry_run: If True, just simulate.
            min_message_id: Stop at this ID (default 1).

        Returns:
            Stats dict.
        """
        self._running = True
        stats = {
            "status": "running",
            "total_checked": 0,
            "videos_found": 0,
            "saved": 0,
            "skipped": 0,
        }

        if job_id:
            update_import_job(
                job_id,
                status="running",
                started_at=datetime.utcnow().isoformat(timespec="seconds"),
            )

        current_id = start_message_id
        if self._last_copied_id:
            current_id = min(current_id, self._last_copied_id - 1)

        log.info(
            "Telethon scan: channel=%s start=%d min=%d",
            channel_id, current_id, min_message_id,
        )

        try:
            if not dry_run:
                client = await _get_telethon_client()
            else:
                client = None

            BATCH_SIZE = 100

            while current_id >= min_message_id and self._running:
                # Build batch of IDs downward
                batch_end = max(current_id - BATCH_SIZE + 1, min_message_id)
                id_range = list(range(current_id, batch_end - 1, -1))
                batch_count = len(id_range)

                stats["total_checked"] += batch_count

                if dry_run:
                    stats["videos_found"] += batch_count
                    stats["saved"] += batch_count
                    self._last_copied_id = batch_end
                    current_id = batch_end - 1
                    if progress and stats["total_checked"] % 500 == 0:
                        await progress(stats)
                    continue

                # Fetch batch silently
                messages = await _batch_fetch(client, channel_id, id_range)
                found_ids = {m.id for m in messages if m}

                # Messages that didn't return = deleted/non-existent
                skipped_in_batch = batch_count - len(messages)
                stats["skipped"] += skipped_in_batch

                for msg in messages:
                    if not msg or not _is_video(msg):
                        stats["skipped"] += 1
                        continue

                    stats["videos_found"] += 1

                    # Extract basic metadata (file_name, caption, duration)
                    filename = None
                    caption = msg.text or ""
                    duration = 0

                    if msg.video:
                        filename = getattr(msg.video, "name", None)
                        attrs = getattr(msg.video, "attributes", [])
                        for attr in attrs:
                            if hasattr(attr, "duration") and attr.duration:
                                duration = int(attr.duration)
                                break
                    elif msg.document:
                        filename = getattr(msg.document, "name", None)
                        for attr in getattr(msg.document, "attributes", []):
                            if hasattr(attr, "duration") and attr.duration:
                                duration = int(attr.duration)
                                break

                    # Use channel_ref: as the file_id — on playback, Bot API
                    # copies from the channel via copyMessage
                    file_ref = f"channel_ref:{channel_id}:{msg.id}"
                    title = title_from_filename_or_caption(
                        filename, caption, f"Video_{msg.id}"
                    )

                    saved = upsert_video(
                        file_id=file_ref,
                        title=title,
                        duration=duration,
                        category=extract_category_from_title(title),
                        source_channel=str(channel_id),
                        source_chat_id=channel_id,
                        source_message_id=msg.id,
                    )
                    stats["saved" if saved else "skipped"] += 1

                # Save checkpoint
                self._last_copied_id = batch_end

                if job_id and stats["total_checked"] % 500 == 0:
                    update_import_job(job_id, **stats)

                if progress and stats["total_checked"] % 100 == 0:
                    await progress(stats)

                # Rate limiting — 2 batches/sec to be gentle
                current_id = batch_end - 1
                await asyncio.sleep(0.5)

            stats["status"] = "done" if self._running else "paused"

        except Exception as exc:
            log.exception("Telethon scan failed at ID %d", current_id)
            stats["status"] = "failed"
            if job_id:
                update_import_job(
                    job_id,
                    **stats,
                    error=str(exc),
                    finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                )
            return stats

        if job_id:
            update_import_job(
                job_id,
                **stats,
                finished_at=datetime.utcnow().isoformat(timespec="seconds"),
            )

        if progress:
            await progress(stats)

        return stats

    def pause(self):
        self._running = False

    @property
    def resume_id(self) -> int:
        return self._last_copied_id
