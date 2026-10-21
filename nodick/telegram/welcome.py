"""Typed welcome banners: Telegram uploads or direct media URLs."""
import json
from urllib.parse import urlsplit

from pathlib import Path

DEFAULT_WELCOME_MEDIA = json.dumps({
    "type": "photo", "media": str(Path(__file__).resolve().parents[2] / "assets" / "welcome-noir.jpg")
})
MEDIA_TYPES = {"photo", "video", "animation"}


def decode_banner(raw):
    if not raw:
        return None
    try:
        banner = json.loads(raw)
        if banner["type"] in MEDIA_TYPES and isinstance(banner["media"], str) and banner["media"]:
            return banner
    except (ValueError, TypeError, KeyError):
        pass
    raise ValueError("Invalid welcome media setting")


def banner_from_message(message):
    """Prefer uploaded media; never interpret a caption as a URL."""
    photos = getattr(message, "photo", None)
    if photos:
        return {"type": "photo", "media": photos[-1].file_id}
    for kind in ("animation", "video"):
        media = getattr(message, kind, None)
        if media:
            return {"type": kind, "media": media.file_id}
    document = getattr(message, "document", None)
    if document:
        mime = document.mime_type or ""
        if mime == "image/gif":
            return {"type": "animation", "media": document.file_id}
        if mime.startswith("image/"):
            return {"type": "photo", "media": document.file_id}
        if mime == "video/mp4":
            return {"type": "video", "media": document.file_id}
    text = (getattr(message, "text", None) or "").strip()
    if text.lower() == "clear":
        return None
    url = urlsplit(text)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("Send a photo, GIF, MP4, or a direct media URL; send clear to disable.")
    path = url.path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".webp")):
        kind = "photo"
    elif path.endswith(".gif"):
        kind = "animation"
    elif path.endswith(".mp4"):
        kind = "video"
    else:
        raise ValueError("Use a direct .jpg/.png/.webp/.gif/.mp4 URL, not a GIF website page. Uploading the file also works.")
    return {"type": kind, "media": text}
