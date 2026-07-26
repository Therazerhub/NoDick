"""NoDick Telegram importer — Telethon-based channel history indexing"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable, Awaitable, Optional

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

from nodick.config import settings
from nodick.db import upsert_video, update_import_job
from nodick.utils import extract_category_from_title, title_from_filename_or_caption

ProgressCallback = Callable[[dict], Awaitable[None]]


def _video_doc(message):
    if getattr(message, "video", None):
        return message.video
    doc = getattr(message, "document", None)
    if doc and getattr(doc, "mime_type", "").startswith("video/"):
        return doc
    return None


def _doc_attrs(doc):
    filename = None
    duration = 0
    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
        elif isinstance(attr, DocumentAttributeVideo):
            duration = int(attr.duration or 0)
        else:
            if hasattr(attr, "file_name") and attr.file_name:
                filename = attr.file_name
            if hasattr(attr, "duration") and attr.duration:
                duration = int(attr.duration)
    return filename, duration


class TelegramImporter:
    """Indexes videos from Telegram history using a user session.

    Bot API cannot read old channel history. A user session can, so NoDick runs
    the importer from inside the bot process and stores stable copy_message refs:
    user_ref:<chat_id>:<message_id>.
    """

    def __init__(self, session_name: Optional[str] = None):
        if not settings.configured_for_user_import:
            raise RuntimeError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH are required for imports"
            )
        self.client = TelegramClient(
            session_name or settings.user_session,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )

    async def ensure_started(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            if settings.telegram_phone:
                await self.client.send_code_request(settings.telegram_phone)
            raise RuntimeError(
                "Telegram user session is not logged in. Run: python -m nodick session-login"
            )
        return self.client

    async def import_channel(
        self,
        channel_id: str,
        *,
        job_id: Optional[int] = None,
        progress: Optional[ProgressCallback] = None,
        limit: Optional[int] = None,
    ) -> dict:
        stats = {
            "total_checked": 0,
            "videos_found": 0,
            "saved": 0,
            "skipped": 0,
            "status": "running",
        }
        if job_id:
            update_import_job(
                job_id,
                status="running",
                started_at=datetime.utcnow().isoformat(timespec="seconds"),
            )
        client = await self.ensure_started()
        entity = await client.get_entity(
            int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id
        )
        source_title = getattr(entity, "title", str(channel_id))
        source_chat_id = (
            int(channel_id)
            if str(channel_id).lstrip("-").isdigit()
            else getattr(entity, "id", None)
        )
        try:
            async for message in client.iter_messages(entity, limit=limit):
                stats["total_checked"] += 1
                doc = _video_doc(message)
                if not doc:
                    if progress and stats["total_checked"] % 500 == 0:
                        await progress(stats)
                    continue
                stats["videos_found"] += 1
                filename, duration = _doc_attrs(doc)
                title = title_from_filename_or_caption(
                    filename,
                    getattr(message, "message", None),
                    f"Video_{message.id}",
                )
                category = extract_category_from_title(title)
                chat_id_for_ref = source_chat_id or getattr(entity, "id", None)
                file_ref = f"user_ref:{chat_id_for_ref}:{message.id}"
                saved = upsert_video(
                    file_id=file_ref,
                    title=title,
                    duration=duration,
                    category=category,
                    source_channel=source_title,
                    source_chat_id=chat_id_for_ref,
                    source_message_id=message.id,
                    file_unique_id=getattr(doc, "id", None)
                    and str(getattr(doc, "id")),
                    file_size=getattr(doc, "size", None),
                )
                stats["saved" if saved else "skipped"] += 1
                if job_id and (stats["videos_found"] % 25 == 0):
                    update_import_job(job_id, **stats)
                if progress and (stats["videos_found"] % 25 == 0):
                    await progress(stats)
                await asyncio.sleep(0.03)
            stats["status"] = "done"
            if job_id:
                update_import_job(
                    job_id,
                    **stats,
                    finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                )
            if progress:
                await progress(stats)
            return stats
        except Exception as exc:
            stats["status"] = "failed"
            if job_id:
                update_import_job(
                    job_id,
                    **stats,
                    error=str(exc),
                    finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                )
            raise

    async def disconnect(self):
        await self.client.disconnect()
