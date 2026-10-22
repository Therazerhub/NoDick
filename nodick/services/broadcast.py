"""Shared Telegram message delivery for manual and autopost broadcasts."""
import asyncio
import logging
from datetime import timedelta

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

log = logging.getLogger(__name__)


async def broadcast_message(bot, source_message, user_ids, *, reply_markup=None, progress=None):
    """Copy a published message once per recipient, with bounded retries.

    Passing the keyboard explicitly is required: copyMessage does not copy an
    inline keyboard by default. Callers retain ownership of run serialization
    and admin reporting; this helper owns pacing and per-recipient failures.
    """
    sent = failed = retried = 0
    recipients = list(dict.fromkeys(int(uid) for uid in user_ids))
    markup = {'reply_markup': reply_markup} if reply_markup is not None else {}
    for index, uid in enumerate(recipients, 1):
        delivered = False
        for attempt in range(3):
            try:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=source_message.chat_id,
                    message_id=source_message.message_id,
                    **markup,
                )
                sent += 1
                delivered = True
                break
            except RetryAfter as exc:
                retried += 1
                retry_after = exc.retry_after
                delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after or 1)
                await asyncio.sleep(delay + 0.25)
            except (Forbidden, BadRequest) as exc:
                log.debug('Broadcast rejected for user %s: %s', uid, type(exc).__name__)
                break
            except (TimedOut, NetworkError):
                retried += 1
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
            except Exception:
                log.exception('Broadcast failed for user %s', uid)
                break
        if not delivered:
            failed += 1
        await asyncio.sleep(0.05)
        if progress is not None and index % 25 == 0:
            try:
                await progress(index, len(recipients))
            except Exception:
                pass
    return sent, failed, retried
