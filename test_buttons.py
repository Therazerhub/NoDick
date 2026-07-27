#!/usr/bin/env python3
"""Test @Moyechan_bot buttons — fixed timing."""

import asyncio, os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_FILE = Path(__file__).parent / "pyrogram_session.txt"
BOT = "@Moyechan_bot"

if not SESSION_FILE.exists():
    print("Session file not found")
    sys.exit(1)

PASS = 0
FAIL = 0


async def send_start_and_wait(app):
    """Send /start and wait for bot to reply with keyboard."""
    await app.send_message(BOT, "/start")
    await asyncio.sleep(3)  # Give bot time to reply


async def get_latest_bot_msg(app):
    """Get the most recent message from the bot."""
    async for msg in app.get_chat_history(BOT, limit=1):
        return msg
    return None


async def click_by_index(app, col, row, label="button", timeout=15):
    """Click a keyboard button by its grid position."""
    global PASS, FAIL
    print("  Click {} (col={}, row={})... ".format(label, col, row), end="", flush=True)
    try:
        target = await get_latest_bot_msg(app)
        if not target or not target.reply_markup:
            print("NO KEYBOARD")
            FAIL += 1
            return None
        result = await target.click(col, row, timeout=timeout)
        print("OK")
        PASS += 1
        return result
    except Exception as e:
        print("FAIL: {}".format(e))
        FAIL += 1
        return None


async def read_response(app):
    """Read the bot's latest response."""
    await asyncio.sleep(2)
    msg = await get_latest_bot_msg(app)
    if not msg:
        return "[no message]"
    if msg.video:
        return "[VIDEO: {}]".format(msg.video.file_id[:25])
    elif msg.text:
        return msg.text[:100].replace('\n', ' | ')
    else:
        return "[other: {}]".format(type(msg).__name__)


async def main():
    from pyrogram import Client

    session_str = SESSION_FILE.read_text().strip()
    async with Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=session_str) as app:
        me = await app.get_me()
        print("Logged in as @{} (id={})".format(me.username, me.id))

        # ── Verify the bot responds with a keyboard ───────────
        print("\n--- Stage 0: Bot responds to /start ---")
        await send_start_and_wait(app)
        msg = await get_latest_bot_msg(app)
        has_kb = msg and msg.reply_markup is not None
        if has_kb:
            btn_count = sum(len(r) for r in msg.reply_markup.inline_keyboard)
            print("  Bot replies with keyboard ({} buttons) ✅".format(btn_count))
        else:
            print("  Bot reply has NO keyboard ❌")
            FAIL += 1

        # Main menu layout:
        # Row 0: [🎲 Surprise Me]
        # Row 1: [🔍 Search, 📁 Categories]
        # Row 2: [⭐ Favorites, 📊 Stats]
        # Row 3: [🛰 Import, ⚙️ Settings]

        # ── 1. Stats ────────────────────────────────────────
        print("\n--- 1. Stats button ---")
        await send_start_and_wait(app)
        await click_by_index(app, 1, 2, "📊 Stats")
        resp = await read_response(app)
        print("  Response: {}".format(resp))

        # ── 2. Categories ───────────────────────────────────
        print("\n--- 2. Categories button ---")
        await send_start_and_wait(app)
        await click_by_index(app, 1, 1, "📁 Categories")
        resp = await read_response(app)
        print("  Response: {}".format(resp))

        # ── 3. Surprise Me (random video) ────────────────────
        print("\n--- 3. Surprise Me ---")
        await send_start_and_wait(app)
        await click_by_index(app, 0, 0, "🎲 Surprise Me")
        resp = await read_response(app)
        print("  Response: {}".format(resp))

        # ── 4. Favorites ─────────────────────────────────────
        print("\n--- 4. Favorites ---")
        await send_start_and_wait(app)
        await click_by_index(app, 0, 2, "⭐ Favorites")
        resp = await read_response(app)
        print("  Response: {}".format(resp))

        # ── 5. Search ────────────────────────────────────────
        print("\n--- 5. Search button ---")
        await send_start_and_wait(app)
        await click_by_index(app, 0, 1, "🔍 Search")
        resp = await read_response(app)
        print("  Response: {}".format(resp))

        # ── Results ──────────────────────────────────────────
        print("\n" + "=" * 50)
        print("RESULTS: {} passed, {} failed".format(PASS, FAIL))
        print("=" * 50)
        if FAIL:
            print("SOME BUTTONS STILL BROKEN")
            sys.exit(1)
        else:
            print("ALL BUTTONS WORKING! 🎉")
            sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
