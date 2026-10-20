import asyncio
import base64
import io
from unittest.mock import AsyncMock
import pytest
from PIL import Image, ImageDraw
from nodick.services import thumbnail_safety as safety

DETECTIONS = [{'class':'BUTTOCKS_EXPOSED','score':.95,'box':[300,200,100,100]}]


def fixture_image():
    im = Image.new('RGB', (960,540), 'white')
    draw = ImageDraw.Draw(im)
    for x in range(260,440,3):
        draw.rectangle((x,160,x+1,340), fill='red' if x % 2 else 'blue')
    out = io.BytesIO(); im.save(out,'PNG')
    return out.getvalue()


def overlay(alpha=0):
    image=Image.new('RGBA',(40,40),(0,255,0,alpha))
    out=io.BytesIO(); image.save(out,'PNG')
    return base64.b64encode(out.getvalue()).decode()


@pytest.mark.parametrize('style',['solid','pixelation','blur','overlay'])
def test_styles_mask_inside_preserve_outside(style):
    data=safety.mask_regions(fixture_image(),DETECTIONS,style,'medium',overlay(255) if style=='overlay' else '')
    image=Image.open(io.BytesIO(data))
    assert min(image.getpixel((100,100))) > 245
    assert data != fixture_image()
    if style=='overlay':
        assert image.getpixel((350,250))[1] > 230


def test_transparent_overlay_has_opaque_backing():
    data=safety.mask_regions(fixture_image(),DETECTIONS,'overlay','medium',overlay(0))
    image=Image.open(io.BytesIO(data))
    assert max(image.getpixel((350,250))) < 30


@pytest.mark.parametrize('encoded',['', 'not-base64', base64.b64encode(b'bad png').decode()])
def test_missing_corrupt_overlay_rejected(encoded):
    with pytest.raises(Exception):
        safety.mask_regions(fixture_image(),DETECTIONS,'overlay','medium',encoded)


def test_configuration_defaults_and_validation(monkeypatch):
    monkeypatch.setattr(safety,'get_bot_setting',lambda key,default='':default)
    assert safety.get_thumbnail_config()==('pixelation','medium','','full_blur')
    monkeypatch.setattr(safety,'get_bot_setting',lambda key,default='':'invalid')
    assert safety.get_thumbnail_config()==('solid','medium','','neutral')


def test_style_strength_overlay_invalidate_cache(monkeypatch):
    safety._cache.clear()
    configuration=['pixelation','medium','','full_blur']
    monkeypatch.setattr(safety,'get_thumbnail_config',lambda:tuple(configuration))
    detector=AsyncMock(return_value=DETECTIONS)
    monkeypatch.setattr(safety,'_detect',detector)
    async def exercise():
        first=await safety.create_censored_thumbnail(fixture_image())
        assert await safety.create_censored_thumbnail(fixture_image())==first
        configuration[0]='solid'
        second=await safety.create_censored_thumbnail(fixture_image())
        assert second!=first
        configuration[:]=['overlay','medium',overlay(255),'full_blur']
        third=await safety.create_censored_thumbnail(fixture_image())
        assert third!=second
        configuration[2]=overlay(0)
        assert await safety.create_censored_thumbnail(fixture_image())!=third
    asyncio.run(exercise())
    assert detector.await_count==4


def test_bad_overlay_and_settings_failure_use_neutral(monkeypatch):
    safety._cache.clear()
    monkeypatch.setattr(safety,'get_thumbnail_config',lambda:('overlay','medium','invalid','full_blur'))
    monkeypatch.setattr(safety,'_detect',AsyncMock(return_value=DETECTIONS))
    assert asyncio.run(safety.create_censored_thumbnail(fixture_image()))==safety.full_blur_thumbnail(fixture_image())


def test_strength_changes_rendering():
    for style in ('pixelation','blur'):
        assert safety.mask_regions(fixture_image(),DETECTIONS,style,'low') != safety.mask_regions(fixture_image(),DETECTIONS,style,'high')
