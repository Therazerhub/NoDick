"""NoDick keyboard layouts — merged from both bots"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from nodick.config import settings
from nodick.db import get_bot_setting


def _action_buttons_enabled() -> bool:
    return get_bot_setting("action_buttons_enabled", "1") == "1"


def main_menu(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎲 Surprise Me", callback_data="random")],
        [
            InlineKeyboardButton("🔍 Search", callback_data="search_menu"),
            InlineKeyboardButton("📁 Categories", callback_data="categories"),
        ],
        [
            InlineKeyboardButton("⭐ Favorites", callback_data="favorites"),
            InlineKeyboardButton("📊 Stats", callback_data="stats"),
        ],
    ]
    if user_id == settings.admin_id:
        rows.append(
            [
                InlineKeyboardButton("🛰 Import", callback_data="import_menu"),
                InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu")]])


def video_actions(
    video_id: int,
    show_rename: bool = False,
    show_duplicate: bool = False,
    feedback_enabled: bool = False,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔥 More", callback_data="random"),
            InlineKeyboardButton("💦 Save", callback_data=f"fav_{video_id}"),
        ]
    ]
    if _action_buttons_enabled():
        phase2_row = []
        if show_rename:
            phase2_row.append(
                InlineKeyboardButton("📝 Rename", callback_data=f"rename_{video_id}")
            )
        if show_duplicate:
            phase2_row.append(
                InlineKeyboardButton("⚠️ Dup", callback_data=f"dup_{video_id}")
            )
        if feedback_enabled:
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


def settings_keyboard() -> InlineKeyboardMarkup:
    enabled = _action_buttons_enabled()
    action_text = "✅ Action Buttons" if enabled else "❌ Action Buttons"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(action_text, callback_data="toggle_action_buttons")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")],
        ]
    )


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
