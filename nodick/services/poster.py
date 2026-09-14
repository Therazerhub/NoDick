import io
import logging
import random
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from nodick.config import settings
from nodick.db import _fetchone, _using_pg, get_bot_setting, get_video
from nodick.metadata.stash import query_api

try:
    from PIL import Image, ImageFilter, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
import requests

log = logging.getLogger(__name__)

def fetch_scene_image(scene_id: str) -> Optional[bytes]:
    if not settings.stash_configured:
        return None
        
    query = """
    query FindScene($id: ID!) {
      findScene(id: $id) {
        images {
          url
        }
      }
    }
    """
    try:
        data = query_api(settings.stashdb_graphql_url, settings.stashdb_api_key, query, {"id": scene_id})
        if data and "findScene" in data and data["findScene"]:
            images = data["findScene"].get("images")
            if images and len(images) > 0:
                url = images[0]["url"]
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    return resp.content
    except Exception as e:
        log.error(f"Failed to fetch scene image for {scene_id}: {e}")
    return None

def create_blurred_thumbnail(image_bytes: bytes) -> bytes:
    if not HAS_PIL:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            # Heavy blur
            blurred = img.filter(ImageFilter.GaussianBlur(50))
            
            # Darken it a bit
            enhancer = Image.eval(blurred, lambda x: int(x * 0.7))
            
            out = io.BytesIO()
            enhancer.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except Exception as e:
        log.error(f"Failed to blur image: {e}")
        return image_bytes

def create_fallback_image(title: str) -> bytes:
    if not HAS_PIL:
        return b""
    img = Image.new('RGB', (800, 450), color=(20, 20, 24))
    try:
        # Just return a raw minimal placeholder if no text font easily available
        out = io.BytesIO()
        img.save(out, format="JPEG")
        return out.getvalue()
    except Exception:
        return b""

async def send_auto_post(bot: Bot, channel_id: str) -> bool:
    # 1. Pick a high quality video from DB (e.g. metadata exists)
    query = """
        SELECT v.id, v.title, v.category, v.tags, m.stashdb_scene_id, m.stashdb_title, m.stashdb_performer, m.stashdb_studio
        FROM videos v
        JOIN video_metadata m ON v.id = m.video_id
        WHERE m.stashdb_scene_id IS NOT NULL
        ORDER BY RANDOM() LIMIT 1
    """
    row = _fetchone(query)
    
    if not row:
        # Fallback to any random video if no metadata
        row = _fetchone("SELECT id, title, category, tags, NULL as stashdb_scene_id, NULL as stashdb_title, NULL as stashdb_performer FROM videos ORDER BY RANDOM() LIMIT 1")
    
    if not row:
        return False
        
    vid_id = row["id"] if _using_pg else row["id"]
    scene_id = row["stashdb_scene_id"] if _using_pg else row["stashdb_scene_id"]
    
    title = row["stashdb_title"] or row["title"] or "Exclusive Video"
    performer = row["stashdb_performer"] or "Unknown"
    category = row["category"] or "Vault"
    
    # 2. Get image
    img_data = None
    if scene_id:
        img_data = fetch_scene_image(scene_id)
        
    if img_data:
        blurred_data = create_blurred_thumbnail(img_data)
    else:
        blurred_data = create_fallback_image(title)
        if not blurred_data:
            return False # Skip if we can't generate an image
            
    # 3. Create Message
    text = (
        f"🎬 **{title}**\n\n"
        f"👤 {performer}\n"
        f"🏷 #{category.replace(' ', '')}\n\n"
        f"❤️ *[ Content Hidden ]*\n\n"
        f"👇 *Tap below to instantly unlock the full Uncensored video.*"
    )
    
    bot_info = await bot.get_me()
    bot_url = f"https://t.me/{bot_info.username}?start=vid_{vid_id}"
    
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Watch in Bot", url=bot_url)]
    ])
    
    try:
        await bot.send_photo(
            chat_id=channel_id,
            photo=blurred_data,
            caption=text,
            parse_mode="Markdown",
            reply_markup=markup
        )
        return True
    except Exception as e:
        log.error(f"Channel post failed: {e}")
        return False
