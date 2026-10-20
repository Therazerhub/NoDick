import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from nodick.services import thumbnail_safety as safety


@pytest.fixture(autouse=True)
def default_config(monkeypatch):
    monkeypatch.setattr(safety, 'get_bot_setting', lambda key, default='': default)


def source():
    out = io.BytesIO()
    Image.new('RGB', (960, 540), (220, 80, 90)).save(out, 'PNG')
    return out.getvalue()


def test_mask_is_opaque_and_padded():
    image = safety.mask_regions(source(), [{'class':'FEMALE_BREAST_EXPOSED','score':.9,'box':[300,200,100,100]}], style='solid')
    result = Image.open(io.BytesIO(image))
    assert result.size == (960, 540)
    assert max(result.getpixel((290, 220))) < 30
    assert result.getpixel((100, 100))[0] > 200


@pytest.mark.parametrize('detections', [[], [{'class':'FACE_FEMALE','score':.99,'box':[1,1,10,10]}], [{'class':'BUTTOCKS_EXPOSED','score':.1,'box':[1,1,10,10]}]])
def test_no_qualified_sensitive_regions_use_neutral_card(detections):
    assert safety.mask_regions(source(), detections) == safety.neutral_thumbnail()


def test_malformed_box_rejected():
    with pytest.raises(ValueError):
        safety.mask_regions(source(), [{'class':'BUTTOCKS_EXPOSED','score':.9,'box':[1,1,float('nan'),10]}])


def test_detector_failure_never_returns_source(monkeypatch):
    safety._cache.clear()
    monkeypatch.setattr(safety,'_detect', AsyncMock(side_effect=RuntimeError('failed')))
    assert asyncio.run(safety.create_censored_thumbnail(source())) == safety.neutral_thumbnail()


def test_invalid_image_never_reaches_detector(monkeypatch):
    safety._cache.clear()
    detector = AsyncMock()
    monkeypatch.setattr(safety, '_detect', detector)
    assert asyncio.run(safety.create_censored_thumbnail(b'invalid')) == safety.neutral_thumbnail()
    detector.assert_not_called()


def test_cache_avoids_repeated_inference(monkeypatch):
    safety._cache.clear()
    detector = AsyncMock(return_value=[])
    monkeypatch.setattr(safety, '_detect', detector)
    async def exercise():
        await safety.create_censored_thumbnail(source())
        await safety.create_censored_thumbnail(source())
    asyncio.run(exercise())
    detector.assert_awaited_once()


def test_worker_timeout_kills_and_reaps(monkeypatch):
    class Worker:
        returncode = None
        killed = False
        waited = False
        async def communicate(self, data):
            await asyncio.sleep(10)
        def kill(self):
            self.killed = True
        async def wait(self):
            self.waited = True
            self.returncode = -9
    worker = Worker()
    monkeypatch.setattr(safety.asyncio, 'create_subprocess_exec', AsyncMock(return_value=worker))
    monkeypatch.setattr(safety, 'DETECTOR_TIMEOUT', .01)
    with pytest.raises(TimeoutError):
        asyncio.run(safety._detect(source()))
    assert worker.killed and worker.waited


def test_real_cpu_worker_on_synthetic_image():
    pytest.importorskip('nudenet')
    # Real inference verifies the shipped model and pinned detector integration;
    # a geometric fixture is NOT an accuracy benchmark for explicit imagery.
    result = asyncio.run(safety._detect(source()))
    assert isinstance(result, list)
