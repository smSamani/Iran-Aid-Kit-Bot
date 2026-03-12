"""One-time Telethon authorization helper for channel access."""

from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import TELETHON_SESSION_PATH, configure_logging, load_settings


async def authorize() -> None:
    """Create the local Telethon user session used by /news."""
    settings = load_settings()
    client = TelegramClient(
        str(TELETHON_SESSION_PATH),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Telethon session is already authorized.")
            return

        phone = settings.telegram_phone or input("Telegram phone number: ").strip()
        await client.send_code_request(phone)
        code = input("Telegram login code: ").strip()

        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = input("Telegram 2FA password: ").strip()
            await client.sign_in(password=password)

        print(f"Telethon session created: {TELETHON_SESSION_PATH}")
    finally:
        await client.disconnect()


def main() -> None:
    """Run the authorization helper."""
    configure_logging()
    asyncio.run(authorize())


if __name__ == "__main__":
    main()
