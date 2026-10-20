"""Conservative selective thumbnail masking with a short-lived CPU worker.

Detection is imperfect: no detections are NOT evidence of safety. Use a
source-free branded card on no sensitive detections, any error, or timeout.
"""
import asyncio
import base64
from collections import OrderedDict
import hashlib
import io
import json
import logging
import math
import os
import sys

from PIL import Image, ImageDraw, ImageOps, ImageFilter
from nodick.db import get_bot_setting

log = logging.getLogger(__name__)
SENSITIVE = {
    'FEMALE_GENITALIA_EXPOSED', 'MALE_GENITALIA_EXPOSED',
    'FEMALE_BREAST_EXPOSED', 'ANUS_EXPOSED', 'BUTTOCKS_EXPOSED',
    'FEMALE_GENITALIA_COVERED', 'ANUS_COVERED',
}
_cache = OrderedDict()
_gate = asyncio.Semaphore(1)
DETECTOR_TIMEOUT = 20


def neutral_thumbnail():
    image = Image.new('RGB', (960, 540), (17, 17, 22))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 40, 920, 500), radius=24, outline=(65, 65, 80), width=2)
    draw.text((410, 235), 'NoDick', fill=(235, 235, 240), font_size=36)
    draw.text((360, 290), 'Preview unavailable', fill=(160, 160, 175), font_size=22)
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=86)
    return out.getvalue()


def prepare_image(raw):
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Thumbnail exceeds input limit')
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 16_000_000:
            raise ValueError('Thumbnail dimensions exceed limit')
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def get_thumbnail_config():
    style = get_bot_setting('thumbnail_style', 'pixelation') or 'pixelation'
    strength = get_bot_setting('thumbnail_strength', 'medium') or 'medium'
    if style not in {'pixelation', 'blur', 'solid', 'overlay'}:
        style = 'solid'
    if strength not in {'low', 'medium', 'high'}:
        strength = 'medium'
    overlay = (get_bot_setting('thumbnail_overlay_png', '') or '') if style == 'overlay' else ''
    return style, strength, overlay


def decode_overlay(encoded):
    if not encoded or len(encoded) > 700_000:
        raise ValueError('Missing or oversized overlay')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 512 * 1024:
        raise ValueError('Oversized overlay')
    with Image.open(io.BytesIO(raw)) as source:
        if source.format != 'PNG' or source.width > 1024 or source.height > 1024 or source.width * source.height > 1_000_000:
            raise ValueError('Invalid overlay image')
        return source.convert('RGBA')


def mask_regions(raw, detections, style='pixelation', strength='medium', overlay_png=''):
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert('RGB')
    original = image.copy()
    # Validate even with no detections: corrupt configuration must fail closed.
    if style not in {'pixelation', 'blur', 'solid', 'overlay'}:
        raise ValueError('Invalid thumbnail style')
    if strength not in {'low', 'medium', 'high'}:
        raise ValueError('Invalid thumbnail strength')
    overlay = decode_overlay(overlay_png) if style == 'overlay' else None
    masked = 0
    for detection in detections:
        if detection.get('class') not in SENSITIVE:
            continue
        score = float(detection.get('score', 0))
        if not math.isfinite(score) or score < 0.25:
            continue
        x, y, w, h = [float(n) for n in detection['box']]
        if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
            raise ValueError('Malformed detection box')
        # Padding is relative to each box with a minimum for small thumbnails.
        px, py = max(12, w * .20), max(12, h * .20)
        box = (max(0, int(x-px)), max(0, int(y-py)),
               min(image.width, math.ceil(x+w+px)), min(image.height, math.ceil(y+h+py)))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError('Detection box outside image')
        size = (box[2] - box[0], box[3] - box[1])
        if style == 'solid' or style == 'overlay':
            region = Image.new('RGB', size, (18, 18, 24))
            if overlay is not None:
                sticker = ImageOps.contain(overlay, size, method=Image.Resampling.LANCZOS)
                offset = ((size[0] - sticker.width) // 2, (size[1] - sticker.height) // 2)
                region.paste(sticker, offset, sticker)
        elif style == 'pixelation':
            # Coarse grid, not a mild resize. Strength sets maximum grid cells
            # across the shortest dimension; each cell spans at least 12px.
            cells = {'low': 8, 'medium': 4, 'high': 2}[strength]
            cell = max(12, math.ceil(min(size) / cells))
            small = (max(1, size[0] // cell), max(1, size[1] // cell))
            region = original.crop(box).resize(small, Image.Resampling.BOX).resize(size, Image.Resampling.NEAREST)
        else:
            radius = max(12, min(size) * {'low': .15, 'medium': .25, 'high': .40}[strength])
            region = original.crop(box).filter(ImageFilter.GaussianBlur(radius))
        image.paste(region, box[:2])
        masked += 1
    if not masked:
        return neutral_thumbnail()
    image = ImageOps.fit(image, (960, 540), method=Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=86)
    return out.getvalue()


async def _detect(raw):
    env = os.environ.copy()
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'nodick.services.thumbnail_detector',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(raw), DETECTOR_TIMEOUT)
        if process.returncode:
            raise RuntimeError(f'NudeNet worker exit {process.returncode}')
        result = json.loads(stdout)
        if not isinstance(result['detections'], list):
            raise ValueError('Invalid detector response')
        log.info('NudeNet worker elapsed=%ss peak_rss_kib=%s', result.get('elapsed_seconds'), result.get('peak_rss_kib'))
        return result['detections']
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def create_censored_thumbnail(raw):
    try:
        config = await asyncio.to_thread(get_thumbnail_config)
    except Exception:
        log.exception('Thumbnail settings unavailable; using neutral card')
        return await asyncio.to_thread(neutral_thumbnail)
    # Style, strength and overlay content all participate: old rendered images
    # must never survive an admin's configuration change.
    key = hashlib.sha256(raw + json.dumps(config).encode()).hexdigest()
    async with _gate:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        try:
            prepared = await asyncio.to_thread(prepare_image, raw)
            detections = await _detect(prepared)
            result = await asyncio.to_thread(mask_regions, prepared, detections, *config)
        except Exception:
            log.exception('Selective thumbnail unavailable; using source-free neutral card')
            return await asyncio.to_thread(neutral_thumbnail)
        _cache[key] = result
        while len(_cache) > 8:
            _cache.popitem(last=False)
        return result
