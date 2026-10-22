"""Autopost fan-out at the Telegram boundary; no live sends or library writes."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, RetryAfter

from nodick.services import poster, thumbnail_safety
from nodick.telegram import autopost


@pytest.fixture
def setup_post(monkeypatch):
    values = {'auto_post_enabled': '1', 'auto_post_channels': '[{"id": -1001}, {"id": -1002}]'}
    for module in (poster, autopost):
        monkeypatch.setattr(module, 'get_bot_setting', lambda key, default='': values.get(key, default))
    monkeypatch.setattr(poster, 'set_bot_setting', values.__setitem__)
    monkeypatch.setattr(poster, '_select_video', lambda: {'id': 42, 'title': 'Example'})
    monkeypatch.setattr(poster, '_fetch_teaser_source', AsyncMock(return_value=(b'source', 'telegram')))
    monkeypatch.setattr(thumbnail_safety, 'create_censored_thumbnail', AsyncMock(return_value=b'censored'))
    # Stub the recipient lookup at its database boundary, including repeated IDs.
    monkeypatch.setattr(poster, 'get_all_user_ids', lambda: [11, 22, 11], raising=False)
    real_sleep = asyncio.sleep
    async def immediate(_delay):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, 'sleep', immediate)
    bot = NS(get_me=AsyncMock(return_value=NS(username='ExampleBot')),
             send_photo=AsyncMock(side_effect=[NS(chat_id=-1001, message_id=7), NS(chat_id=-1002, message_id=8)]),
             copy_message=AsyncMock())
    return values, bot


@pytest.mark.parametrize('manual', [False, True])
def test_scheduled_and_manual_post_copy_one_teaser_per_user(setup_post, manual):
    values, bot = setup_post
    if manual:
        values['auto_post_enabled'] = '0'
        values['auto_post_last'] = datetime.now(timezone.utc).isoformat()
    context = NS(bot=bot, application=NS(bot_data={}))
    assert asyncio.run(autopost._run(context, force=manual)) == 2
    copies = [call.kwargs for call in bot.copy_message.await_args_list]
    assert [c['chat_id'] for c in copies] == [11, 22]
    assert all((c['from_chat_id'], c['message_id']) == (-1001, 7) for c in copies)
    markup = bot.send_photo.await_args_list[0].kwargs['reply_markup']
    assert all(c['reply_markup'] == markup for c in copies)
    assert markup.inline_keyboard[1][0].url == 'https://t.me/ExampleBot?start=vid_42'
    assert bot.send_photo.await_args_list[0].kwargs['photo'] == b'censored'


@pytest.mark.parametrize('reason', ['disabled', 'cooldown', 'no_channels', 'no_video', 'no_art', 'all_failed'])
def test_no_published_post_means_no_private_broadcast(setup_post, monkeypatch, reason):
    values, bot = setup_post
    if reason == 'disabled':
        values['auto_post_enabled'] = '0'
    elif reason == 'cooldown':
        values['auto_post_last'] = datetime.now(timezone.utc).isoformat()
    elif reason == 'no_channels':
        values['auto_post_channels'] = '[]'
    elif reason == 'no_video':
        monkeypatch.setattr(poster, '_select_video', lambda: None)
    elif reason == 'no_art':
        monkeypatch.setattr(poster, '_fetch_teaser_source', AsyncMock(return_value=(None, 'none')))
    else:
        bot.send_photo.side_effect = Forbidden('cannot post')
    context = NS(bot=bot, application=NS(bot_data={}))
    assert asyncio.run(autopost._run(context)) == 0
    bot.copy_message.assert_not_awaited()


def test_partial_channel_success_still_broadcasts_once_and_skips_blocked_user(setup_post):
    _, bot = setup_post
    bot.send_photo.side_effect = [Forbidden('channel unavailable'), NS(chat_id=-1002, message_id=8)]
    bot.copy_message.side_effect = [Forbidden('blocked'), None]
    assert asyncio.run(poster.send_auto_post(bot)) == 1
    assert [c.kwargs['chat_id'] for c in bot.copy_message.await_args_list] == [11, 22]
    assert all(c.kwargs['from_chat_id'] == -1002 for c in bot.copy_message.await_args_list)


def test_autopost_retries_flood_control_without_rebroadcasting_to_previous_user(setup_post):
    _, bot = setup_post
    bot.copy_message.side_effect = [None, RetryAfter(0), None]
    assert asyncio.run(poster.send_auto_post(bot)) == 2
    assert [c.kwargs['chat_id'] for c in bot.copy_message.await_args_list] == [11, 22, 22]


def test_autopost_lock_covers_private_delivery(setup_post):
    _, bot = setup_post
    context = NS(bot=bot, application=NS(bot_data={}))

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def deliver(**kwargs):
            entered.set()
            await release.wait()
        bot.copy_message.side_effect = deliver
        task = asyncio.create_task(autopost._run(context))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            assert await autopost._run(context, force=True) is None
            assert bot.send_photo.await_count == 2
        finally:
            release.set()
            assert await task == 2
        assert [c.kwargs['chat_id'] for c in bot.copy_message.await_args_list] == [11, 22]
    asyncio.run(exercise())


def test_recipient_lookup_failure_does_not_undo_published_post(setup_post, monkeypatch):
    values, bot = setup_post
    def unavailable():
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(poster, 'get_all_user_ids', unavailable, raising=False)
    assert asyncio.run(poster.send_auto_post(bot)) == 2
    assert values['auto_post_last']
    bot.copy_message.assert_not_awaited()
