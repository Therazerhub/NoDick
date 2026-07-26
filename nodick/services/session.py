"""NoDick Telethon session login — CLI command to authenticate user session"""

from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient

from nodick.config import settings


async def login(session_name: str | None = None) -> None:
    """Run the interactive Telethon login flow for user session."""
    session = session_name or settings.user_session
    client = TelegramClient(
        session, settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Already logged in as {me.first_name} (@{me.username or 'no username'})")
        await client.disconnect()
        return

    phone = settings.telegram_phone
    if not phone:
        phone = input("📱 Phone number (with country code): ").strip()

    await client.send_code_request(phone)
    code = input("📨 Enter the code you received: ").strip()

    try:
        await client.sign_in(phone, code)
        me = await client.get_me()
        print(f"✅ Logged in as {me.first_name} (@{me.username or 'no username'})")
    except Exception as e:
        print(f"❌ Login failed: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(login())
