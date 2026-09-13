"""NoDick keyboard layouts — merged from both bots"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from nodick.config import settings
from nodick.db import get_bot_setting, get_user_size_limit, is_user_premium, get_user_quota


def _action_buttons_enabled() -> bool:
    return get_bot_setting("action_buttons_enabled", "1") == "1"


def main_menu(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎲 Surprise Me", callback_data="random")],
        [
            InlineKeyboardButton("🔍 Search", callback_data="search_menu"),
            InlineKeyboardButton("📁 Categories", callback_data="categories"),
        ],
        [InlineKeyboardButton("⭐ Favorites", callback_data="favorites")],
    ]
    
    # Account menu for ALL users
    rows.append([InlineKeyboardButton("👤 My Account", callback_data="my_account")])
    
    extra = get_bot_setting("extra_admins", "")
    is_extra = False
    if extra and user_id is not None:
        try:
            is_extra = user_id in [int(x) for x in extra.split()]
        except ValueError:
            pass

    if user_id == settings.admin_id or is_extra:
        # Add stats next to favorites for admin
        rows[2].append(InlineKeyboardButton("📊 Bot Stats", callback_data="stats"))
        rows.append(
            [
                InlineKeyboardButton("🛰 Import", callback_data="import_menu"),
                InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu")]])


def account_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")],
        [InlineKeyboardButton("🔗 Refer & Earn", callback_data="refer")],
    ]
    if not is_user_premium(user_id):
        rows.append([InlineKeyboardButton("👑 Get Premium", callback_data="get_premium")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def video_actions(
    video_id: int,
    show_rename: bool = False,
    show_duplicate: bool = False,
    feedback_enabled: bool = False,
    show_similar: bool = False,
    performers: list[str] | None = None,
    user_id: int | None = None,
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    
    more_text = "🔥 More"
    if user_id is not None and not is_admin and not is_user_premium(user_id):
        quota = get_user_quota(user_id)
        if quota.get("remaining", 0) < 999999: # Not lifetime
            more_text = f"🔥 More ({quota['remaining']} left)"
            
    rows = [
        [
            InlineKeyboardButton(more_text, callback_data="random"),
            InlineKeyboardButton("💦 Save", callback_data=f"fav_{video_id}"),
        ]
    ]
    # Smart row: recommendations + cast — only when we have cached metadata
    smart_row = []
    if show_similar:
        smart_row.append(
            InlineKeyboardButton("🎯 More Like This", callback_data=f"similar_{video_id}")
        )
    if performers:
        smart_row.append(
            InlineKeyboardButton("👤 Cast", callback_data=f"cast_{video_id}")
        )
    if smart_row:
        rows.append(smart_row)
        
    # Only admins get action buttons like Rename/Dup now
    if _action_buttons_enabled() and is_admin:
        phase2_row = []
        if show_rename:
            phase2_row.append(
                InlineKeyboardButton("📝 Rename", callback_data=f"rename_{video_id}")
            )
        if show_duplicate:
            phase2_row.append(
                InlineKeyboardButton("⚠️ Dup", callback_data=f"dup_{video_id}")
            )
        if feedback_enabled and False:  # Correct button disabled
            phase2_row.append(
                InlineKeyboardButton("✏️ Correct", callback_data=f"correct_{video_id}")
            )
        if phase2_row:
            rows.append(phase2_row)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def import_menu() -> InlineKeyboardMarkup:
    rows = []
    if settings.default_import_channel:
        rows.append(
            [
                InlineKeyboardButton(
                    "📥 Import default", callback_data="import_default"
                )
            ]
        )
    rows += [
        [InlineKeyboardButton("📊 Import status", callback_data="import_status")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(rows)


def scan_running_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Check status", callback_data="import_status")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    enabled = _action_buttons_enabled()
    action_text = "✅ Action Buttons" if enabled else "❌ Action Buttons"
    
    autodel_enabled = get_bot_setting("auto_delete_enabled", "1") == "1"
    autodel_text = "👻 Auto-Delete: ON" if autodel_enabled else "👻 Auto-Delete: OFF"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(action_text, callback_data="toggle_action_buttons")],
        [InlineKeyboardButton("🎬 Quality Limit", callback_data="quality")],
        [
            InlineKeyboardButton(autodel_text, callback_data="toggle_autodelete"),
            InlineKeyboardButton("⏳ Delete Timer", callback_data="prompt_autodelete_timer")
        ],
        [
            InlineKeyboardButton("💰 Payment", callback_data="prompt_payment"),
            InlineKeyboardButton("🎁 Ref Bonus", callback_data="prompt_refbonus")
        ],
        [
            InlineKeyboardButton("📋 Logs Channel", callback_data="prompt_logschannel"),
            InlineKeyboardButton("🔒 Force Join", callback_data="prompt_forcejoin")
        ],
        [InlineKeyboardButton("👥 Co-Admins", callback_data="prompt_coadmins")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")],
    ])


# Max file-size presets (MB). None = unlimited.
QUALITY_OPTIONS: list[tuple[int | None, str]] = [
    (None, "♾️ Unlimited"),
    (500, "📦 Under 500 MB"),
    (1024, "📦 Under 1 GB"),
    (2048, "📦 Under 2 GB"),
    (4096, "📦 Under 4 GB"),
]


def quality_keyboard(user_id: int) -> InlineKeyboardMarkup:
    current = get_user_size_limit(user_id)
    rows = []
    for mb, label in QUALITY_OPTIONS:
        mark = " ✅" if mb == current else ""
        cb = "quality_0" if mb is None else f"quality_{mb}"
        rows.append([InlineKeyboardButton(f"{label}{mark}", callback_data=cb)])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="settings")])
    return InlineKeyboardMarkup(rows)


def force_join_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    """Build inline keyboard with join buttons for each channel + verify button."""
    rows = []
    for ch in channels:
        rows.append([InlineKeyboardButton(f"📢 Join {ch['name']}", url=ch["url"])])
    rows.append([InlineKeyboardButton("✅ I have Joined", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)


def ad_button_keyboard(button_text: str, button_url: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for a sponsored ad message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(button_text, url=button_url)],
    ])


def part_nav_row(siblings: list[dict], video_id: int) -> list[InlineKeyboardButton] | None:
    """Build prev/next navigation row for multi-part videos.

    ``siblings`` is the list from ``find_sibling_parts()``, each with
    ``{id, part, total}``. Returns a list of buttons or None if a single part.
    """
    if not siblings or len(siblings) < 2:
        return None

    current = next((s for s in siblings if s["id"] == video_id), None)
    if not current:
        return None

    # Find actual index in list (handles duplicate part numbers)
    current_idx = next(i for i, s in enumerate(siblings) if s["id"] == video_id)
    total = len(siblings)

    row = []
    if current_idx > 0:
        prev_video = siblings[current_idx - 1]
        row.append(InlineKeyboardButton("◀️", callback_data=f"play_{prev_video['id']}"))

    row.append(InlineKeyboardButton(f"📦 {current_idx + 1}/{total}", callback_data="noop"))

    if current_idx < total - 1:
        next_video = siblings[current_idx + 1]
        row.append(InlineKeyboardButton("▶️", callback_data=f"play_{next_video['id']}"))

    return row
