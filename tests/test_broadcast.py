from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import Forbidden, RetryAfter

from nodick.telegram import app


class _Status:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class _Bot:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.copies = []
        self.messages = []
        self.status = _Status()

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        return self.status

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copies.append((chat_id, from_chat_id, message_id))
        failure = self.failures.get(chat_id)
        if isinstance(failure, list) and failure:
            error = failure.pop(0)
            if error:
                raise error
        elif failure:
            raise failure


class _Application:
    def __init__(self):
        self.bot_data = {}
        self.tasks = []

    def create_task(self, coroutine):
        self.tasks.append(coroutine)
        return coroutine


def _context(bot=None):
    application = _Application()
    return SimpleNamespace(
        bot=bot or _Bot(),
        application=application,
        user_data={"waiting_for_setting": "broadcast"},
    )


def test_broadcast_worker_copies_message_and_reports_completion(monkeypatch):
    bot = _Bot({2: Forbidden("blocked")})
    context = _context(bot)
    monkeypatch.setattr(app, "get_bot_setting", lambda *_args: "")
    real_sleep = asyncio.sleep

    async def immediate(_delay):
        await real_sleep(0)

    monkeypatch.setattr(app.asyncio, "sleep", immediate)
    source = SimpleNamespace(chat_id=99, message_id=7)

    asyncio.run(app._broadcast_worker(context, source, [1, 2], 600))

    assert bot.copies == [(1, 99, 7), (2, 99, 7)]
    assert "Sent: 1" in bot.status.edits[-1]
    assert "Failed: 1" in bot.status.edits[-1]


def test_broadcast_worker_retries_flood_control(monkeypatch):
    bot = _Bot({1: [RetryAfter(0), None]})
    context = _context(bot)
    monkeypatch.setattr(app, "get_bot_setting", lambda *_args: "")
    real_sleep = asyncio.sleep

    async def immediate(_delay):
        await real_sleep(0)

    monkeypatch.setattr(app.asyncio, "sleep", immediate)
    source = SimpleNamespace(chat_id=99, message_id=7)

    asyncio.run(app._broadcast_worker(context, source, [1], 600))

    assert bot.copies == [(1, 99, 7), (1, 99, 7)]
    assert "Sent: 1" in bot.status.edits[-1]
    assert "Retried: 1" in bot.status.edits[-1]


def test_broadcast_input_queues_copy_worker(monkeypatch):
    context = _context()
    replies = []
    monkeypatch.setattr(app, "_is_admin", lambda _update: True)
    monkeypatch.setattr(app, "get_all_user_ids", lambda: [3, 3, 4])
    message = SimpleNamespace(
        chat_id=600,
        message_id=12,
        reply_text=lambda text: _async_append(replies, text),
    )
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=600))

    try:
        asyncio.run(app.broadcast_input(update, context))
    except app.ApplicationHandlerStop:
        pass

    assert context.user_data.get("waiting_for_setting") is None
    assert len(context.application.tasks) == 1
    assert "2 users" in replies[0]
    context.application.tasks[0].close()


async def _async_append(target, value):
    target.append(value)
