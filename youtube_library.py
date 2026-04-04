"""Lightweight per-user YouTube library persistence."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class YouTubeLibraryStore:
    """Persist a small per-user YouTube library in one local JSON file."""

    def __init__(
        self,
        storage_path: Path,
        *,
        max_saved_channels: int = 25,
        max_saved_playlists: int = 25,
        max_watch_later_items: int = 40,
        max_recent_searches: int = 8,
    ) -> None:
        self._storage_path = storage_path
        self._lock = threading.Lock()
        self._max_saved_channels = max_saved_channels
        self._max_saved_playlists = max_saved_playlists
        self._max_watch_later_items = max_watch_later_items
        self._max_recent_searches = max_recent_searches

    def load_user_library(self, user_key: str) -> dict[str, list[dict[str, object]]]:
        """Return a defensive copy of the current user library snapshot."""
        with self._lock:
            payload = self._load_all()
            library = self._ensure_user_library(payload, user_key)
            return copy.deepcopy(library)

    def save_channel(self, user_key: str, channel_payload: dict[str, object]) -> bool:
        """Save one channel and return whether it was newly added."""
        identity = self._channel_identity(channel_payload)
        if not identity:
            raise ValueError("Missing channel identity.")
        entry = copy.deepcopy(channel_payload)
        entry["key"] = self._make_key("channel", identity)
        entry["saved_at"] = self._timestamp()
        return self._upsert_item(
            user_key,
            "saved_channels",
            entry,
            key_field="key",
            limit=self._max_saved_channels,
        )

    def save_playlist(self, user_key: str, playlist_payload: dict[str, object]) -> bool:
        """Save one playlist and return whether it was newly added."""
        identity = self._playlist_identity(playlist_payload)
        if not identity:
            raise ValueError("Missing playlist identity.")
        entry = copy.deepcopy(playlist_payload)
        entry["key"] = self._make_key("playlist", identity)
        entry["saved_at"] = self._timestamp()
        return self._upsert_item(
            user_key,
            "saved_playlists",
            entry,
            key_field="key",
            limit=self._max_saved_playlists,
        )

    def save_watch_later(
        self,
        user_key: str,
        *,
        video_payload: dict[str, object],
        playlist_payload: dict[str, object] | None = None,
    ) -> bool:
        """Save one video for watch-later and return whether it was newly added."""
        identity = self._video_identity(video_payload)
        if not identity:
            raise ValueError("Missing video identity.")
        entry: dict[str, object] = {
            "key": self._make_key("watch", identity),
            "saved_at": self._timestamp(),
            "video": copy.deepcopy(video_payload),
        }
        if playlist_payload is not None:
            entry["playlist"] = copy.deepcopy(playlist_payload)
        return self._upsert_item(
            user_key,
            "watch_later",
            entry,
            key_field="key",
            limit=self._max_watch_later_items,
        )

    def add_recent_search(self, user_key: str, query: str, *, scope: str) -> None:
        """Push one query to the recent-search list."""
        normalized_query = " ".join(query.strip().split())
        if not normalized_query:
            return
        entry: dict[str, object] = {
            "key": self._make_key("recent", f"{scope}:{normalized_query.casefold()}"),
            "query": normalized_query,
            "scope": scope,
            "saved_at": self._timestamp(),
        }
        self._upsert_item(
            user_key,
            "recent_searches",
            entry,
            key_field="key",
            limit=self._max_recent_searches,
        )

    def get_saved_channel(self, user_key: str, key: str) -> dict[str, object] | None:
        """Return one saved channel entry by its stable key."""
        return self._find_item(user_key, "saved_channels", key)

    def get_saved_playlist(self, user_key: str, key: str) -> dict[str, object] | None:
        """Return one saved playlist entry by its stable key."""
        return self._find_item(user_key, "saved_playlists", key)

    def get_watch_later(self, user_key: str, key: str) -> dict[str, object] | None:
        """Return one watch-later entry by its stable key."""
        return self._find_item(user_key, "watch_later", key)

    def get_recent_search(self, user_key: str, key: str) -> dict[str, object] | None:
        """Return one recent-search entry by its stable key."""
        return self._find_item(user_key, "recent_searches", key)

    def remove_saved_channel(self, user_key: str, key: str) -> bool:
        """Remove one saved channel by key."""
        return self._remove_item(user_key, "saved_channels", key)

    def remove_saved_playlist(self, user_key: str, key: str) -> bool:
        """Remove one saved playlist by key."""
        return self._remove_item(user_key, "saved_playlists", key)

    def remove_watch_later(self, user_key: str, key: str) -> bool:
        """Remove one watch-later entry by key."""
        return self._remove_item(user_key, "watch_later", key)

    def _find_item(
        self,
        user_key: str,
        bucket_name: str,
        key: str,
    ) -> dict[str, object] | None:
        with self._lock:
            payload = self._load_all()
            library = self._ensure_user_library(payload, user_key)
            bucket = library.get(bucket_name, [])
            if not isinstance(bucket, list):
                return None
            for item in bucket:
                if isinstance(item, dict) and str(item.get("key") or "") == key:
                    return copy.deepcopy(item)
        return None

    def _remove_item(
        self,
        user_key: str,
        bucket_name: str,
        key: str,
    ) -> bool:
        with self._lock:
            payload = self._load_all()
            library = self._ensure_user_library(payload, user_key)
            bucket = library.get(bucket_name, [])
            if not isinstance(bucket, list):
                return False
            remaining = [
                item
                for item in bucket
                if not (isinstance(item, dict) and str(item.get("key") or "") == key)
            ]
            if len(remaining) == len(bucket):
                return False
            library[bucket_name] = remaining
            self._save_all(payload)
            return True

    def _upsert_item(
        self,
        user_key: str,
        bucket_name: str,
        entry: dict[str, object],
        *,
        key_field: str,
        limit: int,
    ) -> bool:
        with self._lock:
            payload = self._load_all()
            library = self._ensure_user_library(payload, user_key)
            bucket = library.setdefault(bucket_name, [])
            if not isinstance(bucket, list):
                bucket = []
                library[bucket_name] = bucket

            entry_key = str(entry.get(key_field) or "")
            existed = any(
                isinstance(item, dict) and str(item.get(key_field) or "") == entry_key
                for item in bucket
            )
            bucket[:] = [
                item
                for item in bucket
                if not (isinstance(item, dict) and str(item.get(key_field) or "") == entry_key)
            ]
            bucket.insert(0, entry)
            del bucket[limit:]
            self._save_all(payload)
            return not existed

    def _load_all(self) -> dict[str, object]:
        if not self._storage_path.exists():
            return {"version": 1, "users": {}}
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "users": {}}
        if not isinstance(payload, dict):
            return {"version": 1, "users": {}}
        payload.setdefault("version", 1)
        users = payload.get("users")
        if not isinstance(users, dict):
            payload["users"] = {}
        return payload

    def _save_all(self, payload: dict[str, object]) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._storage_path.with_suffix(f"{self._storage_path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self._storage_path)

    @staticmethod
    def _ensure_user_library(
        payload: dict[str, object],
        user_key: str,
    ) -> dict[str, list[dict[str, object]]]:
        users = payload.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            payload["users"] = users
        library = users.setdefault(
            user_key,
            {
                "saved_channels": [],
                "saved_playlists": [],
                "watch_later": [],
                "recent_searches": [],
            },
        )
        if not isinstance(library, dict):
            library = {
                "saved_channels": [],
                "saved_playlists": [],
                "watch_later": [],
                "recent_searches": [],
            }
            users[user_key] = library
        for name in ("saved_channels", "saved_playlists", "watch_later", "recent_searches"):
            if not isinstance(library.get(name), list):
                library[name] = []
        return library

    @staticmethod
    def _channel_identity(channel_payload: dict[str, object]) -> str | None:
        for key in ("channel_id", "channel_url", "uploader_url", "webpage_url"):
            value = channel_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _playlist_identity(playlist_payload: dict[str, object]) -> str | None:
        for key in ("playlist_id", "webpage_url"):
            value = playlist_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _video_identity(video_payload: dict[str, object]) -> str | None:
        for key in ("video_id", "webpage_url"):
            value = video_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _make_key(kind: str, identity: str) -> str:
        digest = hashlib.sha1(f"{kind}:{identity}".encode("utf-8")).hexdigest()
        return digest[:12]

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
