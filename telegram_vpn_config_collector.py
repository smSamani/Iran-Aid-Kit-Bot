"""Collect VPN configs from Telegram sources and package them for download."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageEntityTextUrl,
    MessageEntityUrl,
)

from config import DOWNLOADS_DIR, TELETHON_SESSION_PATH


VPN_SOURCE_URLS = (
    "https://t.me/ParsiNetFree",
    "https://t.me/nftvici",
    "https://t.me/V2rayEnglish",
    "https://t.me/IraneAzad_Net",
    "https://t.me/erfwp",
    "https://t.me/proxyxix",
    "https://t.me/xixv2ray",
    "https://t.me/RUSSIAPROXYY",
)
VPN_ARCHIVE_PASSWORD_ENV_KEY = "VPN_ARCHIVE_PASSWORD"
VPN_ARCHIVE_NAME = "BasteyeHemayati.zip"
VPN_LOOKBACK_HOURS = 48
VPN_FETCH_MESSAGE_LIMIT = 600

SLIPNET_PATTERN = re.compile(r"slipnet(?:-enc)?://[^\s<>'\"“”]+", re.IGNORECASE)
VLESS_PATTERN = re.compile(r"vless://[^\s<>'\"“”]+", re.IGNORECASE)
PROXY_PATTERN = re.compile(
    r"(?:https?://t\.me/proxy\?[^\s<>'\"“”]+|tg://proxy\?[^\s<>'\"“”]+)",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,;:!?)>]}»،؛"


class TelegramVpnConfigAuthorizationError(RuntimeError):
    """Raised when the Telethon user session is missing or unauthorized."""


@dataclass(slots=True)
class TelegramVpnBundle:
    """Packaged VPN config bundle plus extraction stats."""

    archive_path: Path
    staging_dir: Path
    slipnet_count: int
    proxy_count: int
    vless_count: int
    npvt_count: int
    scanned_message_count: int


class TelegramVpnConfigCollector:
    """Collect recent VPN configs from Telegram channels/groups."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        source_urls: tuple[str, ...] = VPN_SOURCE_URLS,
        password: str | None = None,
        lookback_hours: int = VPN_LOOKBACK_HOURS,
    ) -> None:
        self._client = TelegramClient(str(TELETHON_SESSION_PATH), api_id, api_hash)
        self._source_usernames = tuple(
            username
            for username in (self._extract_username(url) for url in source_urls)
            if username
        )
        resolved_password = str(password or os.getenv(VPN_ARCHIVE_PASSWORD_ENV_KEY, "")).strip()
        if not resolved_password:
            raise ValueError(
                "VPN_ARCHIVE_PASSWORD is required before packaging the VPN support bundle."
            )
        self._password = resolved_password
        self._lookback_hours = lookback_hours
        self._lock = asyncio.Lock()

    @staticmethod
    def _extract_username(source_url: str) -> str:
        parsed = urlparse(source_url)
        path = parsed.path.strip("/")
        if not path:
            return source_url.strip().removeprefix("@")
        return path.split("/", maxsplit=1)[0]

    @staticmethod
    def _clean_candidate(candidate: str) -> str:
        return candidate.strip().strip(TRAILING_PUNCTUATION)

    @staticmethod
    def _safe_file_name(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._")
        return cleaned or "file.npvt"

    @staticmethod
    def _unique_append(items: list[str], seen: set[str], value: str) -> None:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            items.append(cleaned)

    @staticmethod
    def _message_file_name(message: Message) -> str | None:
        file_name = getattr(getattr(message, "file", None), "name", None)
        if isinstance(file_name, str) and file_name.strip():
            return file_name.strip()
        document = getattr(message, "document", None)
        for attribute in getattr(document, "attributes", None) or []:
            if isinstance(attribute, DocumentAttributeFilename):
                candidate = (attribute.file_name or "").strip()
                if candidate:
                    return candidate
        return None

    def _extract_text_configs(
        self,
        message: Message,
        *,
        slipnet: list[str],
        seen_slipnet: set[str],
        proxies: list[str],
        seen_proxies: set[str],
        vless: list[str],
        seen_vless: set[str],
    ) -> None:
        text = getattr(message, "message", None) or ""
        if text:
            for candidate in SLIPNET_PATTERN.findall(text):
                self._unique_append(slipnet, seen_slipnet, self._clean_candidate(candidate))
            for candidate in VLESS_PATTERN.findall(text):
                self._unique_append(vless, seen_vless, self._clean_candidate(candidate))
            for candidate in PROXY_PATTERN.findall(text):
                self._unique_append(proxies, seen_proxies, self._clean_candidate(candidate))

        for entity, inner_text in message.get_entities_text(MessageEntityTextUrl):
            candidate = self._clean_candidate(getattr(entity, "url", "") or "")
            if not candidate:
                continue
            if SLIPNET_PATTERN.fullmatch(candidate):
                self._unique_append(slipnet, seen_slipnet, candidate)
            elif VLESS_PATTERN.fullmatch(candidate):
                self._unique_append(vless, seen_vless, candidate)
            elif PROXY_PATTERN.fullmatch(candidate):
                self._unique_append(proxies, seen_proxies, candidate)

        for _entity, inner_text in message.get_entities_text(MessageEntityUrl):
            candidate = self._clean_candidate(inner_text or "")
            if not candidate:
                continue
            if SLIPNET_PATTERN.fullmatch(candidate):
                self._unique_append(slipnet, seen_slipnet, candidate)
            elif VLESS_PATTERN.fullmatch(candidate):
                self._unique_append(vless, seen_vless, candidate)
            elif PROXY_PATTERN.fullmatch(candidate):
                self._unique_append(proxies, seen_proxies, candidate)

        reply_markup = getattr(message, "reply_markup", None)
        for row in getattr(reply_markup, "rows", None) or []:
            for button in getattr(row, "buttons", None) or []:
                candidate = self._clean_candidate(getattr(button, "url", "") or "")
                if not candidate:
                    continue
                if SLIPNET_PATTERN.fullmatch(candidate):
                    self._unique_append(slipnet, seen_slipnet, candidate)
                elif VLESS_PATTERN.fullmatch(candidate):
                    self._unique_append(vless, seen_vless, candidate)
                elif PROXY_PATTERN.fullmatch(candidate):
                    self._unique_append(proxies, seen_proxies, candidate)

    async def _download_npvt_if_present(
        self,
        message: Message,
        *,
        source_username: str,
        npvt_dir: Path,
    ) -> bool:
        file_name = self._message_file_name(message)
        if not file_name or not file_name.lower().endswith(".npvt"):
            return False
        safe_name = self._safe_file_name(file_name)
        destination = npvt_dir / f"{source_username}_{message.id}_{safe_name}"
        result = await self._client.download_media(message, file=str(destination))
        return bool(result)

    @staticmethod
    def _write_config_file(path: Path, entries: list[str], *, blank_line_between: bool) -> None:
        separator = "\n\n" if blank_line_between else "\n"
        content = separator.join(entries).strip()
        if content:
            content += "\n"
        path.write_text(content, encoding="utf-8")

    async def _create_password_zip(self, staging_dir: Path, archive_path: Path) -> None:
        members = ["slipnet.txt", "proxy.txt", "vless.txt", "npvt"]
        if archive_path.exists():
            archive_path.unlink()
        process = await asyncio.create_subprocess_exec(
            shutil.which("zip") or "/usr/bin/zip",
            "-rq",
            "-P",
            self._password,
            str(archive_path),
            *members,
            cwd=str(staging_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Failed to build password-protected archive: {(stderr or b'').decode('utf-8', 'ignore').strip()}"
            )

    async def collect_bundle(self) -> TelegramVpnBundle:
        """Collect the last 48h of configs and package them into a zip archive."""
        async with self._lock:
            await self._client.connect()
            try:
                if not await self._client.is_user_authorized():
                    raise TelegramVpnConfigAuthorizationError(
                        "Telethon session is not authorized. Run `python auth_telethon.py` first."
                    )

                cutoff = datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)
                staging_dir = DOWNLOADS_DIR / f"vpn-configs-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
                npvt_dir = staging_dir / "npvt"
                staging_dir.mkdir(parents=True, exist_ok=True)
                npvt_dir.mkdir(parents=True, exist_ok=True)

                slipnet: list[str] = []
                proxies: list[str] = []
                vless: list[str] = []
                seen_slipnet: set[str] = set()
                seen_proxies: set[str] = set()
                seen_vless: set[str] = set()
                scanned_message_count = 0
                npvt_count = 0

                for source_username in self._source_usernames:
                    async for message in self._client.iter_messages(
                        source_username,
                        limit=VPN_FETCH_MESSAGE_LIMIT,
                    ):
                        message_date = getattr(message, "date", None)
                        if message_date is None:
                            continue
                        scanned_message_count += 1
                        if message_date.astimezone(timezone.utc) < cutoff:
                            break

                        self._extract_text_configs(
                            message,
                            slipnet=slipnet,
                            seen_slipnet=seen_slipnet,
                            proxies=proxies,
                            seen_proxies=seen_proxies,
                            vless=vless,
                            seen_vless=seen_vless,
                        )
                        if await self._download_npvt_if_present(
                            message,
                            source_username=source_username,
                            npvt_dir=npvt_dir,
                        ):
                            npvt_count += 1

                self._write_config_file(
                    staging_dir / "slipnet.txt",
                    slipnet,
                    blank_line_between=True,
                )
                self._write_config_file(
                    staging_dir / "proxy.txt",
                    proxies,
                    blank_line_between=True,
                )
                self._write_config_file(
                    staging_dir / "vless.txt",
                    vless,
                    blank_line_between=True,
                )

                archive_path = DOWNLOADS_DIR / VPN_ARCHIVE_NAME
                await self._create_password_zip(staging_dir, archive_path)
                return TelegramVpnBundle(
                    archive_path=archive_path,
                    staging_dir=staging_dir,
                    slipnet_count=len(slipnet),
                    proxy_count=len(proxies),
                    vless_count=len(vless),
                    npvt_count=npvt_count,
                    scanned_message_count=scanned_message_count,
                )
            except TelegramVpnConfigAuthorizationError:
                raise
            except RPCError as exc:
                raise RuntimeError("Telethon failed while collecting VPN configs.") from exc
            finally:
                await self._client.disconnect()
