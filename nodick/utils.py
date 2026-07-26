"""NoDick utilities — title cleaning, category extraction, helpers"""

from __future__ import annotations

import os
import re
from typing import Optional

DOMAIN_CATEGORIES: dict[str, str] = {
    "brazzers": "Brazzers",
    "brazzer": "Brazzers",
    "pornhub": "Pornhub",
    "ph": "Pornhub",
    "xvideos": "XVideos",
    "xnxx": "XNXX",
    "youporn": "YouPorn",
    "redtube": "RedTube",
    "spankbang": "SpankBang",
    "chaturbate": "Chaturbate",
    "onlyfans": "OnlyFans",
    "of": "OnlyFans",
    "manyvids": "ManyVids",
    "clips4sale": "Clips4Sale",
    "realitykings": "Reality Kings",
    "bangbros": "BangBros",
    "naughtyamerica": "Naughty America",
    "digitalplayground": "Digital Playground",
    "mofos": "Mofos",
    "teamskeet": "TeamSkeet",
    "vixen": "Vixen",
    "tushy": "Tushy",
    "blacked": "Blacked",
    "blackedraw": "Blacked Raw",
    "deeper": "Deeper",
    "julesjordan": "Jules Jordan",
    "evilangel": "Evil Angel",
    "sweetsinner": "Sweet Sinner",
    "milfed": "Milfed",
    "penthouse": "Penthouse",
    "21sextury": "21Sextury",
    "slayed": "Slayed",
    "tushyraw": "Tushy Raw",
}


def clean_title_for_display(title: str) -> str:
    """Clean a raw filename/title for safe display."""
    if not title:
        return "Untitled"
    clean = re.sub(
        r"\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg|part\d+)$", "",
        title,
        flags=re.I,
    )
    clean = re.sub(
        r"\b\w+\.(com|net|org|cc|co\.\w+|tv|xxx)\b", "", clean, flags=re.I
    )
    clean = re.sub(r"[\[\]\(\)\{\}]", "", clean)
    clean = clean.replace(".", " ").replace("_", " ").replace("-", " ")
    clean = re.sub(r"\b(19|20)\d{2}\b", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "Untitled"


def title_from_filename_or_caption(
    filename: Optional[str], caption: Optional[str], fallback: str
) -> str:
    """Generate a display title from filename or caption."""
    if filename:
        return clean_title_for_display(os.path.splitext(filename)[0])[:180]
    if caption:
        return clean_title_for_display(caption.split("\n", 1)[0])[:180]
    return fallback


def extract_category_from_title(title: str) -> Optional[str]:
    """Extract category/source from a filename or title string."""
    if not title:
        return None
    title_lower = title.lower()

    # Try bracket/ paren patterns
    for pattern in [
        r"\[([^\]]+)\]",
        r"\(([^\)]+)\)",
        r"www\.([a-z0-9]+)",
        r"([a-z0-9]+)\.com",
        r"([a-z0-9]+)\.net",
    ]:
        for match in re.findall(pattern, title_lower):
            key = match.strip().replace(".", "").replace("-", "").replace("_", "")
            if key in DOMAIN_CATEGORIES:
                return DOMAIN_CATEGORIES[key]

    # Direct keyword match
    for keyword, category in DOMAIN_CATEGORIES.items():
        if keyword in title_lower:
            return category
    return None


def format_duration(seconds: Optional[int]) -> str:
    """Format seconds to m:ss"""
    if not seconds:
        return "?"
    return f"{seconds // 60}:{seconds % 60:02d}"
