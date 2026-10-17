from __future__ import annotations

import asyncio

import pytest

from nodick.services import message_importer


def test_batch_fetch_retries_transient_errors(monkeypatch):
    attempts = []

    class Client:
        async def get_messages(self, channel_id, *, ids):
            attempts.append((channel_id, ids))
            if len(attempts) < 3:
                raise RuntimeError("temporary")
            return ["ok"]

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(message_importer.asyncio, "sleep", no_sleep)
    result = asyncio.run(
        message_importer._batch_fetch(Client(), -1001, [10, 9, 8])
    )
    assert result == ["ok"]
    assert len(attempts) == 3


def test_batch_fetch_honors_flood_wait_seconds(monkeypatch):
    sleeps = []

    class FloodWait(Exception):
        seconds = 7

    class Client:
        def __init__(self):
            self.calls = 0

        async def get_messages(self, channel_id, *, ids):
            self.calls += 1
            if self.calls == 1:
                raise FloodWait("slow down")
            return ["ok"]

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(message_importer.asyncio, "sleep", record_sleep)
    result = asyncio.run(
        message_importer._batch_fetch(Client(), -1001, [10, 9, 8])
    )
    assert result == ["ok"]
    assert sleeps == [8.0]


def test_batch_fetch_raises_after_bounded_retries(monkeypatch):
    attempts = []

    class Client:
        async def get_messages(self, channel_id, *, ids):
            attempts.append((channel_id, ids))
            raise RuntimeError("offline")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(message_importer.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(
            message_importer._batch_fetch(Client(), -1001, [10, 9, 8])
        )
    assert len(attempts) == 3


def test_dry_run_reports_next_checkpoint(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(message_importer.asyncio, "sleep", no_sleep)
    importer = message_importer.MessageIDImporter()
    result = asyncio.run(
        importer.import_channel(
            channel_id=-1001,
            start_message_id=205,
            min_message_id=101,
            dry_run=True,
        )
    )
    assert result["status"] == "done"
    assert result["total_checked"] == 105
    assert result["next_message_id"] == 100
