import asyncio
import io
from unittest.mock import AsyncMock
import pytest
from PIL import Image, ImageStat
from nodick.services import thumbnail_safety as safety
from test_poster import _source_jpeg


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    safety._cache.clear()
    monkeypatch.setattr(safety,'get_bot_setting',lambda key,default='':default)


@pytest.mark.parametrize('outcome',[[],RuntimeError('worker failed'),TimeoutError('worker timeout')])
@pytest.mark.parametrize('fallback',['full_blur','neutral','skip'])
def test_no_regions_and_worker_errors_obey_fallback(monkeypatch,outcome,fallback):
    config=('pixelation','medium','',fallback)
    monkeypatch.setattr(safety,'get_thumbnail_config',lambda:config)
    detector=AsyncMock(side_effect=outcome) if isinstance(outcome,Exception) else AsyncMock(return_value=outcome)
    monkeypatch.setattr(safety,'_detect',detector)
    raw=_source_jpeg()
    actual=asyncio.run(safety.create_censored_thumbnail(raw))
    expected=safety.fallback_thumbnail(raw,fallback,'medium')
    assert actual==expected
    assert actual!=raw
    if fallback=='full_blur':
        image=Image.open(io.BytesIO(actual))
        assert image.size==(960,540)
        assert sum(ImageStat.Stat(image).stddev)/3 > 8
        assert actual!=safety.neutral_thumbnail()


def test_primary_full_blur_skips_detector(monkeypatch):
    monkeypatch.setattr(safety,'get_thumbnail_config',lambda:('full_blur','high','','neutral'))
    detector=AsyncMock(side_effect=AssertionError('must not load NudeNet'))
    monkeypatch.setattr(safety,'_detect',detector)
    assert asyncio.run(safety.create_censored_thumbnail(_source_jpeg()))==safety.full_blur_thumbnail(_source_jpeg(),'high')
    detector.assert_not_awaited()


def test_invalid_image_cannot_be_full_blurred_or_return_raw(monkeypatch):
    detector=AsyncMock()
    monkeypatch.setattr(safety,'_detect',detector)
    assert asyncio.run(safety.create_censored_thumbnail(b'not an image'))==b''
    detector.assert_not_awaited()


def test_fallback_change_invalidates_cached_card(monkeypatch):
    config=['pixelation','medium','','neutral']
    monkeypatch.setattr(safety,'get_thumbnail_config',lambda:tuple(config))
    monkeypatch.setattr(safety,'_detect',AsyncMock(return_value=[]))
    async def exercise():
        assert await safety.create_censored_thumbnail(_source_jpeg())==safety.neutral_thumbnail()
        config[3]='full_blur'
        image=await safety.create_censored_thumbnail(_source_jpeg())
        assert image==safety.full_blur_thumbnail(_source_jpeg())
        config[3]='skip'
        assert await safety.create_censored_thumbnail(_source_jpeg())==b''
    asyncio.run(exercise())


def test_settings_unavailable_skips_instead_of_unselected_card(monkeypatch):
    def unavailable(): raise RuntimeError('database unavailable')
    monkeypatch.setattr(safety,'get_thumbnail_config',unavailable)
    assert asyncio.run(safety.create_censored_thumbnail(_source_jpeg()))==b''
