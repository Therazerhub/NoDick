import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from nodick.services import poster,message_importer
from test_poster import _source_jpeg


@pytest.mark.parametrize('video',[
    {'id':1,'source_chat_id':'-1001234','source_message_id':'88'},
    {'id':1,'file_id':'channel_ref:-1001234:88'},
])
def test_telegram_numeric_ids_and_legacy_references(monkeypatch,video):
    message=NS(media=object())
    async def download(msg,*,file,thumb):
        assert msg is message and thumb==-1
        file.write(_source_jpeg())
    client=NS(get_messages=AsyncMock(return_value=message),download_media=download)
    monkeypatch.setattr(message_importer,'_get_telethon_client',AsyncMock(return_value=client))
    assert asyncio.run(poster.fetch_telegram_thumb(video))==_source_jpeg()
    client.get_messages.assert_awaited_once_with(-1001234,ids=88)


def test_stash_tries_next_artwork_variant_after_invalid_body(monkeypatch):
    monkeypatch.setattr(poster,'settings',NS(stash_configured=True,stashdb_graphql_url='https://stash.example/graphql',stashdb_api_key='test-only-key'))
    monkeypatch.setattr(poster,'query_api',lambda *args:{'data':{'findScene':{'images':[
        {'url':'https://images.example/large.jpg','width':1920,'height':1080},
        {'url':'https://images.example/small.jpg','width':640,'height':360},
    ]}}})
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        return NS(status_code=200,content=b'HTML error page' if 'large' in url else _source_jpeg())
    monkeypatch.setattr(poster.requests,'get',get)
    assert poster.fetch_scene_image('scene')==_source_jpeg()
    assert calls==['https://images.example/large.jpg','https://images.example/small.jpg']


def test_invalid_stash_body_does_not_block_valid_telegram_thumb(monkeypatch):
    monkeypatch.setattr(poster,'fetch_scene_image',lambda scene:b'<html>Access denied</html>')
    telegram=AsyncMock(return_value=_source_jpeg())
    monkeypatch.setattr(poster,'fetch_telegram_thumb',telegram)
    assert asyncio.run(poster._fetch_teaser_source({'id':1,'stashdb_scene_id':'scene'}))==(_source_jpeg(),'telegram')
    telegram.assert_awaited_once()


def test_invalid_sources_are_not_passed_to_renderer(monkeypatch):
    monkeypatch.setattr(poster,'fetch_scene_image',lambda scene:b'not-image')
    monkeypatch.setattr(poster,'fetch_telegram_thumb',AsyncMock(return_value=b'bad thumb'))
    assert asyncio.run(poster._fetch_teaser_source({'id':1,'stashdb_scene_id':'scene'}))==(None,'none')


def test_valid_stash_source_does_not_need_telegram_credentials(monkeypatch):
    monkeypatch.setattr(poster,'fetch_scene_image',lambda scene:_source_jpeg())
    telegram=AsyncMock(side_effect=AssertionError('not needed'))
    monkeypatch.setattr(poster,'fetch_telegram_thumb',telegram)
    assert asyncio.run(poster._fetch_teaser_source({'id':1,'stashdb_scene_id':'scene'}))==(_source_jpeg(),'stashdb')
    telegram.assert_not_awaited()


def test_telegram_download_has_cancellation_deadline(monkeypatch):
    cancelled=[]
    async def slow(video):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)
    monkeypatch.setattr(poster,'_fetch_telegram_thumb',slow)
    monkeypatch.setattr(poster,'TELEGRAM_THUMB_TIMEOUT',.01)
    assert asyncio.run(poster.fetch_telegram_thumb({'id':1})) is None
    assert cancelled==[True]


def test_legacy_blur_never_returns_raw_when_pillow_unavailable(monkeypatch):
    monkeypatch.setattr(poster,'HAS_PIL',False)
    assert poster.create_blurred_thumbnail(_source_jpeg())==b''
