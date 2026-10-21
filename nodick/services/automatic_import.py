"""Bounded, restart-safe polling of the admin-selected channel's new messages.

Run ``poll_new_videos`` from a PTB JobQueue repeating job. This intentionally
uses a separate high-water cursor from the manual backwards scan checkpoint.
"""

from __future__ import annotations

import asyncio
import logging

from nodick.config import settings
from nodick.db import get_bot_setting, set_bot_setting
from nodick.services.message_importer import MessageIDImporter, _get_telethon_client

log = logging.getLogger(__name__)

# At most three 100-ID fetches per tick. Backlog drains on subsequent ticks.
MAX_IDS_PER_POLL = 300
_lock = asyncio.Lock()


def _selected_channel() -> int | None:
    value = get_bot_setting("scan_channel_id") or settings.default_import_channel
    try:
        channel_id = int(value)
        return channel_id if channel_id < 0 else None
    except (ValueError, TypeError):
        return None


async def poll_new_videos() -> dict:
    """Import only new IDs; never move the cursor past an unsuccessful batch.

    A newly selected channel imports its current latest message (not history).
    Failures propagate to the scheduler logger, leaving the cursor safe to retry.
    Requires API_ID/HASH and bot access to the selected channel's history.
    """
    if _lock.locked():
        return {"status": "busy"}
    async with _lock:
        channel_id = _selected_channel()
        if channel_id is None:
            return {"status": "unconfigured"}
        if not (settings.telegram_api_id and settings.telegram_api_hash):
            return {"status": "credentials_missing"}

        client = await _get_telethon_client()
        # A history read is necessary: Bot API getUpdates cannot discover
        # messages published while the process was asleep.
        latest_messages = await client.get_messages(channel_id, limit=1)
        latest_id = max((m.id for m in latest_messages if m), default=0)
        if not latest_id:
            return {"status": "empty"}

        # Keep cursor and channel identity in one atomic setting. A crash
        # between two separate writes could otherwise skip another channel.
        cursor_key = f"auto_import_cursor_{channel_id}"
        cursor_raw = get_bot_setting(cursor_key, "__unset__")
        try:
            cursor = max(0, int(cursor_raw or 0))
        except (TypeError, ValueError):
            cursor = 0
        # Fresh selection: only index the latest available message. Returning
        # to a previously selected channel catches up from its own cursor.
        if cursor_raw == "__unset__":
            cursor = latest_id - 1
            # Persist the baseline before touching Telegram/DB so a failed
            # first attempt still retries this same message on the next tick.
            set_bot_setting(cursor_key, str(cursor))
        if latest_id <= cursor:
            return {"status": "current", "cursor": cursor}

        end = min(latest_id, cursor + MAX_IDS_PER_POLL)
        # The manual importer scans downwards. Run one bounded batch at a time
        # ascending, so completed lower batches are checkpointed in order.
        saved = 0
        checked = 0
        for low in range(cursor + 1, end + 1, 100):
            high = min(low + 99, end)
            result = await MessageIDImporter().import_channel(
                channel_id, high, min_message_id=low,
            )
            if result["status"] != "done":
                raise RuntimeError(f"Automatic import failed at {low}-{high}: {result['status']}")
            # Settings may change while an API call is in flight. Never write a
            # cursor for the newly selected channel with the old channel's ID.
            if _selected_channel() != channel_id:
                return {"status": "channel_changed", "checked": checked, "saved": saved}
            set_bot_setting(cursor_key, str(high))
            checked += result["total_checked"]
            saved += result["saved"]
        log.info("Automatic import channel=%s checked=%d saved=%d cursor=%d", channel_id, checked, saved, end)
        return {"status": "done", "checked": checked, "saved": saved, "cursor": end,
                "backlog": latest_id > end}


async def automatic_import_job(context) -> None:
    """PTB JobQueue callback; report failure without disabling future ticks."""
    try:
        result = await poll_new_videos()
        if result["status"] in {"credentials_missing", "unconfigured"}:
            log.warning("Automatic import inactive: %s", result["status"])
    except Exception:
        log.exception("Automatic channel import failed; next tick will retry")
