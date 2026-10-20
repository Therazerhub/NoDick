"""Deadline regressions, including a real local trickle-response server."""
import asyncio
import time
from unittest.mock import AsyncMock
import pytest
from nodick.services import shortener
from test_shortener_service import config


@pytest.mark.parametrize('stage', ['queue','dns'])
def test_deadline_includes_queue_and_dns(config,monkeypatch,stage):
    monkeypatch.setattr(shortener,'PROVIDER_DEADLINE',.03)
    async def slow_dns(host):
        await asyncio.sleep(5)
    if stage=='queue':
        monkeypatch.setattr(shortener,'_api_gate',asyncio.Semaphore(0))
    else:
        monkeypatch.setattr(shortener,'_api_gate',asyncio.Semaphore(4))
        monkeypatch.setattr(shortener,'_check_dns',slow_dns)
    began=time.monotonic()
    with pytest.raises(shortener.ShortenerError):
        asyncio.run(shortener.shorten_destination('https://t.me/TestBot?start=pass_'+'a'*32))
    assert time.monotonic()-began < 1


def test_real_trickle_response_is_closed_on_total_deadline(config,monkeypatch):
    monkeypatch.setattr(shortener,'PROVIDER_DEADLINE',.12)
    monkeypatch.setattr(shortener,'_api_gate',asyncio.Semaphore(4))
    async def exercise():
        disconnected=asyncio.Event()
        async def serve(reader,writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n')
                await writer.drain()
                for _ in range(100):
                    writer.write(b'1\r\nx\r\n')
                    await writer.drain()
                    await asyncio.sleep(.01)
            except (ConnectionError, asyncio.IncompleteReadError):
                disconnected.set()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
        server=await asyncio.start_server(serve,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        monkeypatch.setattr(shortener,'provider_endpoint',lambda:f'http://127.0.0.1:{port}/api')
        try:
            began=time.monotonic()
            with pytest.raises(shortener.ShortenerError):
                await shortener.shorten_destination('https://t.me/TestBot?start=pass_'+'a'*32)
            assert time.monotonic()-began < 1
            await asyncio.wait_for(disconnected.wait(),1)
            assert shortener._api_gate._value==4
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())
