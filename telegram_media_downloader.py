"""MTProto media download fallback for bot messages."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError

from config import TELETHON_BOT_MEDIA_SESSION_PATH


class TelegramMediaDownloader:
    """Download message media through Telethon using the bot token."""

    def __init__(self, api_id: int, api_hash: str, bot_token: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        self._lock = asyncio.Lock()
        self._client = TelegramClient(
            str(TELETHON_BOT_MEDIA_SESSION_PATH),
            api_id,
            api_hash,
        )

    async def download_message_media(
        self,
        *,
        chat_id: int,
        message_id: int,
        destination: Path,
    ) -> None:
        """Download media for a bot-visible message to the given path."""
        async with self._lock:
            await self._client.connect()
            try:
                if not await self._client.is_user_authorized():
                    await self._client.start(bot_token=self._bot_token)

                message = await self._client.get_messages(chat_id, ids=message_id)
                if message is None or getattr(message, "media", None) is None:
                    raise RuntimeError("Media was not found in the Telegram message.")

                result = await self._client.download_media(message, file=str(destination))
                if not result:
                    raise RuntimeError("Telethon did not return a downloaded file.")
            except RPCError as exc:
                raise RuntimeError("Telethon failed to download the Telegram media.") from exc
            finally:
                await self._client.disconnect()
