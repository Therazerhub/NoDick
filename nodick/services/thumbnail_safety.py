"""Conservative selective thumbnail masking with a short-lived CPU worker.

Detection is imperfect: no detections are NOT evidence of safety. Use a
source-free branded card on no sensitive detections, any error, or timeout.
"""
import asyncio
from collections import OrderedDict
import hashlib
import io
import json
import logging
import math
import os
import sys

from PIL import Image, ImageDraw, ImageOps

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


def mask_regions(raw, detections):
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert('RGB')
    draw = ImageDraw.Draw(image)
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
        draw.rectangle(box, fill=(18, 18, 24))
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
    key = hashlib.sha256(raw).hexdigest()
    async with _gate:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        try:
            prepared = await asyncio.to_thread(prepare_image, raw)
            detections = await _detect(prepared)
            result = await asyncio.to_thread(mask_regions, prepared, detections)
        except Exception:
            log.exception('Selective thumbnail unavailable; using source-free neutral card')
            return await asyncio.to_thread(neutral_thumbnail)
        _cache[key] = result
        while len(_cache) > 8:
            _cache.popitem(last=False)
        return result
