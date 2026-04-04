"""Persistent Rubika-only access control state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from config import BASE_DIR


RUBIKA_ACCESS_CONTROL_FILE = BASE_DIR / "rubika_access_control.json"
ALLOWED_STATUSES = {"pending", "allowed", "suspended", "blocked"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class RubikaAccessControl:
    """File-backed allow/block/admin state for Rubika users."""

    def __init__(self, *, file_path: Path | None = None, owner_ids: Iterable[str] = ()) -> None:
        self._file_path = file_path or RUBIKA_ACCESS_CONTROL_FILE
        self._lock = Lock()
        self._state = self._load_state()
        self._merge_owner_ids(owner_ids)

    def _default_state(self) -> dict[str, Any]:
        return {
            "owner_ids": [],
            "admin_ids": [],
            "users": {},
        }

    def _load_state(self) -> dict[str, Any]:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            return self._default_state()
        try:
            payload = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default_state()
        if not isinstance(payload, dict):
            return self._default_state()
        state = self._default_state()
        state["owner_ids"] = self._normalize_id_list(payload.get("owner_ids"))
        state["admin_ids"] = self._normalize_id_list(payload.get("admin_ids"))
        state["users"] = self._normalize_users(payload.get("users"))
        return state

    def _normalize_id_list(self, values: Any) -> list[str]:
        result: list[str] = []
        if not isinstance(values, list):
            return result
        for value in values:
            cleaned = _clean_str(value)
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result

    def _normalize_users(self, value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for raw_user_id, raw_record in value.items():
            user_id = _clean_str(raw_user_id)
            if user_id is None or not isinstance(raw_record, dict):
                continue
            normalized[user_id] = self._normalize_user_record(user_id, raw_record)
        return normalized

    def _normalize_user_record(self, user_id: str, record: dict[str, Any]) -> dict[str, Any]:
        status = _clean_str(record.get("status")) or "pending"
        if status not in ALLOWED_STATUSES:
            status = "pending"
        return {
            "user_id": user_id,
            "chat_id": _clean_str(record.get("chat_id")) or user_id,
            "display_name": _clean_str(record.get("display_name")) or "",
            "status": status,
            "first_seen_at": _clean_str(record.get("first_seen_at")) or _utc_now_iso(),
            "last_seen_at": _clean_str(record.get("last_seen_at")) or _utc_now_iso(),
            "last_start_at": _clean_str(record.get("last_start_at")) or "",
            "start_count": int(record.get("start_count") or 0),
        }

    def _save_locked(self) -> None:
        self._file_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _merge_owner_ids(self, owner_ids: Iterable[str]) -> None:
        changed = False
        with self._lock:
            for owner_id in owner_ids:
                cleaned = _clean_str(owner_id)
                if cleaned is None:
                    continue
                if cleaned not in self._state["owner_ids"]:
                    self._state["owner_ids"].append(cleaned)
                    changed = True
                user_record = self._ensure_user_locked(cleaned, chat_id=cleaned, display_name=None)
                if user_record["status"] != "allowed":
                    user_record["status"] = "allowed"
                    changed = True
            if changed:
                self._save_locked()

    def _ensure_user_locked(
        self,
        user_id: str,
        *,
        chat_id: str | None,
        display_name: str | None,
    ) -> dict[str, Any]:
        existing = self._state["users"].get(user_id)
        if existing is None:
            existing = self._normalize_user_record(
                user_id,
                {
                    "chat_id": chat_id or user_id,
                    "display_name": display_name or "",
                },
            )
            self._state["users"][user_id] = existing
        if chat_id:
            existing["chat_id"] = chat_id
        if display_name:
            existing["display_name"] = display_name
        return existing

    def record_seen(
        self,
        *,
        user_id: str,
        chat_id: str,
        display_name: str | None,
        started: bool,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        with self._lock:
            record = self._ensure_user_locked(user_id, chat_id=chat_id, display_name=display_name)
            record["last_seen_at"] = now
            if started:
                record["last_start_at"] = now
                record["start_count"] = int(record.get("start_count") or 0) + 1
            self._save_locked()
            return dict(record)

    def bootstrap_owner(
        self,
        *,
        user_id: str,
        chat_id: str,
        display_name: str | None,
    ) -> bool:
        with self._lock:
            if self._state["owner_ids"]:
                return False
            self._state["owner_ids"].append(user_id)
            record = self._ensure_user_locked(user_id, chat_id=chat_id, display_name=display_name)
            record["status"] = "allowed"
            self._save_locked()
            return True

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._state["users"].get(user_id)
            return dict(record) if record is not None else None

    def resolve_user_id(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
    ) -> str | None:
        """Resolve the canonical access-control key for one actor.

        This keeps older Rubika records keyed by chat id working while newer
        records may be keyed by the actual user id.
        """
        candidates: list[str] = []
        for value in (user_id, chat_id):
            cleaned = _clean_str(value)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
        if not candidates:
            return None

        with self._lock:
            for candidate in candidates:
                if (
                    candidate in self._state["users"]
                    or candidate in self._state["owner_ids"]
                    or candidate in self._state["admin_ids"]
                ):
                    return candidate

            cleaned_chat_id = _clean_str(chat_id)
            if cleaned_chat_id is not None:
                for stored_user_id, record in self._state["users"].items():
                    if _clean_str(record.get("chat_id")) == cleaned_chat_id:
                        return stored_user_id

        return candidates[0]

    def list_users(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            users = [dict(record) for record in self._state["users"].values()]
        if status is not None:
            users = [record for record in users if record.get("status") == status]
        users.sort(key=lambda record: (record.get("last_seen_at") or "", record.get("user_id") or ""), reverse=True)
        return users

    def list_staff(self) -> list[dict[str, Any]]:
        staff_ids = self.owner_ids() + [user_id for user_id in self.admin_ids() if user_id not in self.owner_ids()]
        records: list[dict[str, Any]] = []
        for user_id in staff_ids:
            record = self.get_user(user_id) or {
                "user_id": user_id,
                "chat_id": user_id,
                "display_name": "",
                "status": "allowed",
                "first_seen_at": "",
                "last_seen_at": "",
                "last_start_at": "",
                "start_count": 0,
            }
            records.append(record)
        return records

    def owner_ids(self) -> list[str]:
        with self._lock:
            return list(self._state["owner_ids"])

    def admin_ids(self) -> list[str]:
        with self._lock:
            return list(self._state["admin_ids"])

    def staff_ids(self) -> list[str]:
        result = self.owner_ids()
        for admin_id in self.admin_ids():
            if admin_id not in result:
                result.append(admin_id)
        return result

    def notification_chat_ids(self) -> list[str]:
        targets: list[str] = []
        for user_id in self.staff_ids():
            record = self.get_user(user_id)
            chat_id = _clean_str(record.get("chat_id")) if record else None
            if chat_id and chat_id not in targets:
                targets.append(chat_id)
        return targets

    def is_owner(self, user_id: str) -> bool:
        return user_id in self.owner_ids()

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admin_ids()

    def is_staff(self, user_id: str) -> bool:
        return self.is_owner(user_id) or self.is_admin(user_id)

    def is_allowed(self, user_id: str) -> bool:
        if self.is_staff(user_id):
            return True
        record = self.get_user(user_id)
        return bool(record and record.get("status") == "allowed")

    def set_user_status(
        self,
        user_id: str,
        *,
        status: str,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported status: {status}")
        with self._lock:
            record = self._ensure_user_locked(user_id, chat_id=chat_id or user_id, display_name=None)
            record["status"] = status
            self._save_locked()
            return dict(record)

    def add_admin(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._ensure_user_locked(user_id, chat_id=user_id, display_name=None)
            record["status"] = "allowed"
            if user_id not in self._state["owner_ids"] and user_id not in self._state["admin_ids"]:
                self._state["admin_ids"].append(user_id)
            self._save_locked()
            return dict(record)

    def remove_admin(self, user_id: str) -> bool:
        with self._lock:
            if user_id in self._state["owner_ids"]:
                return False
            if user_id not in self._state["admin_ids"]:
                return False
            self._state["admin_ids"] = [admin_id for admin_id in self._state["admin_ids"] if admin_id != user_id]
            self._save_locked()
            return True

    def reset_for_owner(
        self,
        *,
        owner_id: str,
        chat_id: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Clear all access state and keep only the requesting owner."""
        with self._lock:
            owner_record = self._normalize_user_record(
                owner_id,
                {
                    "chat_id": chat_id or owner_id,
                    "display_name": display_name or "",
                    "status": "allowed",
                    "last_start_at": _utc_now_iso(),
                    "start_count": 1,
                },
            )
            self._state = {
                "owner_ids": [owner_id],
                "admin_ids": [],
                "users": {
                    owner_id: owner_record,
                },
            }
            self._save_locked()
            return dict(owner_record)
