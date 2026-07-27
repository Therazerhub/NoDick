#!/usr/bin/env python3
"""Debug: check what BotCallbackAnswer returns for each button."""

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


async def main():
    from pyrogram import Client

    session_str = SESSION_FILE.read_text().strip()
    async with Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=session_str) as app:
        me = await app.get_me()
        print("Logged in as @{} (id={})".format(me.username, me.id))

        async def click_and_dump(label, col, row):
            """Click a button and dump what comes back."""
            print("\n--- {} ---".format(label))
            # Fresh menu
            await app.send_message(BOT, "/start")
            await asyncio.sleep(3)

            async for msg in app.get_chat_history(BOT, limit=1):
                target = msg
                break

            if not target or not target.reply_markup:
                print("  NO KEYBOARD")
                return

            print("  Keyboard message: id={}, text='{}'".format(
                target.id, (target.text or "").replace('\n', ' | ')[:60]
            ))

            # Click
            result = await target.click(col, row, timeout=15)
            await asyncio.sleep(2)

            # Dump result
            print("  Result type: {}".format(type(result).__name__))
            if hasattr(result, 'alert') and result.alert:
                print("  Alert: {}".format(result.alert))
            if hasattr(result, 'message') and result.message:
                m = result.message
                if hasattr(m, 'id'):
                    print("  Result message: id={}, text='{}'".format(
                        m.id, (m.text or "").replace('\n', ' | ')[:80]
                    ))
                    if m.reply_markup:
                        print("  Result has keyboard: {} buttons".format(
                            sum(len(r) for r in m.reply_markup.inline_keyboard)
                        ))
                else:
                    print("  Result message: (string) '{}'".format(str(m)[:80]))

            # Latest message in chat
            async for msg in app.get_chat_history(BOT, limit=1):
                latest = msg
                break
            if latest:
                print("  Latest chat msg: id={}, text='{}'".format(
                    latest.id, (latest.text or "").replace('\n', ' | ')[:80]
                ))
                if latest.reply_markup:
                    print("  Latest has keyboard: {} buttons".format(
                        sum(len(r) for r in latest.reply_markup.inline_keyboard)
                    ))

        # Test a few buttons
        await click_and_dump("Stats (1,2)", 1, 2)
        await click_and_dump("Categories (1,1)", 1, 1)
        await click_and_dump("Surprise Me (0,0)", 0, 0)
        await click_and_dump("Search (0,1)", 0, 1)

        print("\n✅ Done")


if __name__ == "__main__":
    asyncio.run(main())
