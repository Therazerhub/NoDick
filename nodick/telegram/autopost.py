"""Admin-visible autopost state and serialized scheduler/manual execution."""
import asyncio
from datetime import datetime, timezone
import json
import logging

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest
from nodick.db import get_bot_setting, set_bot_setting
from nodick.services import poster

log = logging.getLogger(__name__)
JOB_NAME = 'nodick_autopost_hourly'
KICK_NAME = 'nodick_autopost_enabled_check'


def _data(context):
    return context.application.bot_data


def status_keyboard():
    enabled = get_bot_setting('auto_post_enabled', '0') == '1'
    return Markup([
        [Button('Auto-Post: ' + ('ON' if enabled else 'OFF'), callback_data='toggle_autopost'),
         Button('Refresh', callback_data='autopost_status')],
        [Button('Post Now', callback_data='prompt_autopostnow'),
         Button('Cooldown', callback_data='prompt_autopost_cooldown')],
        [Button('Channels', callback_data='prompt_autopost_channels'),
         Button('Settings', callback_data='settings')],
    ])


def last_result():
    try:
        value = json.loads(get_bot_setting('auto_post_last_result', '') or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _stamp(raw):
    if not raw:
        return 'Not recorded'
    try:
        value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime('%d %b %Y, %H:%M:%S UTC')
    except (ValueError, TypeError):
        return 'Invalid saved timestamp'


def status_text(context):
    enabled = get_bot_setting('auto_post_enabled', '0') == '1'
    channels = poster._get_channels()
    queue = context.job_queue
    scheduler = getattr(queue, 'scheduler', None) if queue else None
    jobs = list(queue.get_jobs_by_name(JOB_NAME)) if queue else []
    if queue is None:
        scheduler_state = 'UNAVAILABLE — JobQueue missing'
    elif not jobs:
        scheduler_state = 'NOT REGISTERED — restart/update required'
    elif not getattr(scheduler, 'running', False):
        scheduler_state = 'Registered, not started'
    else:
        scheduler_state = 'RUNNING (checks hourly while the service is awake)'
    upcoming = []
    for job in jobs + (list(queue.get_jobs_by_name(KICK_NAME)) if queue else []):
        try:
            next_time = job.next_t
            if next_time:
                upcoming.append(next_time)
        except (AttributeError, RuntimeError):
            pass
    next_check = _stamp(min(upcoming)) if upcoming else 'Not available until scheduler starts'
    remaining = poster.cooldown_seconds_remaining()
    hours, seconds = divmod(max(0, int(remaining)), 3600)
    minutes, seconds = divmod(seconds, 60)
    cooldown = f'{hours}h {minutes}m {seconds}s remaining' if remaining else 'Ready (cooldown elapsed)'
    result = last_result()
    reason = str(result.get('reason') or 'No scheduler/manual check recorded yet.')
    text = (
        'Auto-Post Status\n\n'
        f"Automatic posting: {'ON' if enabled else 'OFF'}\n"
        f'Scheduler: {scheduler_state}\n'
        f'Destinations: {len(channels)} channel(s)\n'
        f'Cooldown: {cooldown}\n'
        f'Last successful post: {_stamp(get_bot_setting("auto_post_last", ""))}\n'
        f'Last check: {_stamp(result.get("checked_at"))}\n'
        f'Last result: {reason}\n'
        f"Last attempt destinations: sent {poster._safe_count(result.get('successes', 0))}, failed {poster._safe_count(result.get('failed', 0))}\n"
        f'Next scheduler check: {next_check}\n'
    )
    if _data(context).get('autopost_running'):
        text += '\nA post attempt is currently running.\n'
    if not channels:
        text += '\nBlocked: no posting channels are configured.\n'
    if not enabled:
        text += '\nAutomatic posting is OFF. Post Now still works manually.\n'
    text += '\nRender Free sleep pauses this scheduler; ON does not mean the service is always awake.'
    # Untrusted saved names/errors must never become Telegram Markdown entities.
    return text


async def show_status(update, context, notice=''):
    from nodick.telegram import app
    if not app._is_admin(update):
        await update.callback_query.answer('Admin only.', show_alert=True)
        return
    await update.callback_query.answer()
    text = (notice + '\n\n' if notice else '') + status_text(context)
    try:
        await app._replace_with_text(update, context, text, status_keyboard(), parse_mode=None)
    except BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise


def _record(status, reason):
    try:
        poster.record_post_result(status, reason)
    except Exception:
        log.exception('Could not record auto-post state')


async def _run(context, force=False):
    data = _data(context)
    lock = data.setdefault('autopost_run_lock', asyncio.Lock())
    if lock.locked():
        return None
    async with lock:
        if not force and get_bot_setting('auto_post_enabled', '0') != '1':
            _record('disabled', 'disabled')
            return 0
        data['autopost_running'] = True
        try:
            return await poster.send_auto_post(context.bot, force=force)
        except Exception:
            log.exception('Auto-post attempt failed')
            _record('error', 'error')
            return 0
        finally:
            data['autopost_running'] = False


async def scheduled_check(context):
    await _run(context)


def setup_jobs(application):
    queue = application.job_queue
    if queue:
        queue.run_repeating(scheduled_check, interval=3600, first=60, name=JOB_NAME,
                            job_kwargs={'max_instances': 1, 'coalesce': True, 'misfire_grace_time': 300})
    else:
        log.warning('JobQueue unavailable: automatic posting cannot run')


async def toggle(update, context):
    from nodick.telegram import app
    if not app._is_admin(update):
        await update.callback_query.answer('Admin only.', show_alert=True)
        return
    data = _data(context)
    lock = data.setdefault('autopost_toggle_lock', asyncio.Lock())
    async with lock:
        target = '0' if get_bot_setting('auto_post_enabled', '0') == '1' else '1'
        set_bot_setting('auto_post_enabled', target)
        saved = get_bot_setting('auto_post_enabled', '0')
        if saved != target:
            await show_status(update, context, 'Could not save the Auto-Post setting. State below is the saved value.')
            return
        notice = 'Auto-Post is now ' + ('ON.' if target == '1' else 'OFF.')
        queue = context.job_queue
        if queue:
            for job in queue.get_jobs_by_name(KICK_NAME):
                job.schedule_removal()
            if target == '1':
                queue.run_once(scheduled_check, when=1, name=KICK_NAME)
                notice += ' A cooldown-aware check has been queued now.'
        elif target == '1':
            notice += ' Warning: JobQueue is unavailable, so automatic posting cannot run.'
        await show_status(update, context, notice)


async def post_now(update, context):
    from nodick.telegram import app
    if not app._is_admin(update):
        await update.callback_query.answer('Admin only.', show_alert=True)
        return
    await update.callback_query.answer()
    await app._replace_with_text(update, context, 'Posting to the configured channels…', status_keyboard(), parse_mode=None)
    successes = await _run(context, force=True)
    if successes is None:
        notice = 'Another post attempt is already running. Refresh to see its result.'
    elif successes:
        notice = f'Posted successfully to {successes} channel(s).'
    else:
        notice = 'No post was sent. See Last result below for the reason.'
    await app._replace_with_text(update, context, notice + '\n\n' + status_text(context), status_keyboard(), parse_mode=None)
