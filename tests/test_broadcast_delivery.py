import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from nodick.services.broadcast import broadcast_message


@pytest.mark.parametrize('wait', [65, timedelta(seconds=65)])
def test_flood_wait_is_honored_in_full(monkeypatch, wait):
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    bot = NS(copy_message=AsyncMock(side_effect=[RetryAfter(wait), None]))
    result = asyncio.run(broadcast_message(bot, NS(chat_id=-1001, message_id=7), [11]))
    assert result == (1, 0, 1)
    assert delays == [65.25, 0.05]


def test_rejected_message_is_not_retried_and_next_recipient_still_receives(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    bot = NS(copy_message=AsyncMock(side_effect=[BadRequest('chat not found'), None]))
    result = asyncio.run(broadcast_message(bot, NS(chat_id=99, message_id=7), [11, 22]))
    assert result == (1, 1, 0)
    assert [c.kwargs['chat_id'] for c in bot.copy_message.await_args_list] == [11, 22]


def test_network_retries_are_bounded_and_later_users_still_receive(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    bot = NS(copy_message=AsyncMock(side_effect=[TimedOut(), TimedOut(), TimedOut(), None]))
    result = asyncio.run(broadcast_message(bot, NS(chat_id=99, message_id=7), [11, 22]))
    assert result == (1, 1, 3)
    assert [c.kwargs['chat_id'] for c in bot.copy_message.await_args_list] == [11, 11, 11, 22]


def test_empty_audience_does_not_send():
    bot = NS(copy_message=AsyncMock())
    assert asyncio.run(broadcast_message(bot, NS(chat_id=99, message_id=7), [])) == (0, 0, 0)
    bot.copy_message.assert_not_awaited()


def test_progress_failure_does_not_interrupt_delivery(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    progress = AsyncMock(side_effect=RuntimeError('status deleted'))
    bot = NS(copy_message=AsyncMock())
    result = asyncio.run(broadcast_message(bot, NS(chat_id=99, message_id=7), range(1, 27), progress=progress))
    assert result == (26, 0, 0)
    assert [c.kwargs['chat_id'] for c in bot.copy_message.await_args_list] == list(range(1, 27))
    progress.assert_awaited_once_with(25, 26)
