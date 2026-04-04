"""Shared Gemini API key pool with simple rotation and per-key stats."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock
from typing import Any, Sequence

from google import genai
from google.genai import errors, types


LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 503}


def _mask_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


@dataclass(slots=True)
class GeminiKeyStats:
    """Mutable per-key counters for Gemini traffic."""

    label: str
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_model: str = ""
    last_error: str = ""


class GeminiClientPool:
    """Route Gemini requests across multiple API keys."""

    def __init__(self, api_keys: Sequence[str]) -> None:
        cleaned_keys: list[str] = []
        for raw_key in api_keys:
            key = str(raw_key or "").strip()
            if key and key not in cleaned_keys:
                cleaned_keys.append(key)
        if not cleaned_keys:
            raise ValueError("At least one Gemini API key is required.")

        self._clients = [genai.Client(api_key=key) for key in cleaned_keys]
        self._lock = Lock()
        self._next_index = 0
        self._stats = [GeminiKeyStats(label=_mask_key(key)) for key in cleaned_keys]

    def key_count(self) -> int:
        """Return how many API keys are available in the pool."""
        return len(self._clients)

    def stats_snapshot(self) -> list[dict[str, Any]]:
        """Return a JSON-serializable snapshot of per-key counters."""
        with self._lock:
            return [
                {
                    "label": stat.label,
                    "request_count": stat.request_count,
                    "success_count": stat.success_count,
                    "failure_count": stat.failure_count,
                    "last_model": stat.last_model,
                    "last_error": stat.last_error,
                }
                for stat in self._stats
            ]

    def _ordered_key_indices(self) -> list[int]:
        with self._lock:
            start_index = self._next_index
            self._next_index = (self._next_index + 1) % len(self._clients)
        return [
            (start_index + offset) % len(self._clients)
            for offset in range(len(self._clients))
        ]

    def _record_request(self, key_index: int, model_name: str) -> None:
        with self._lock:
            stat = self._stats[key_index]
            stat.request_count += 1
            stat.last_model = model_name
            stat.last_error = ""

    def _record_success(self, key_index: int, model_name: str) -> None:
        with self._lock:
            stat = self._stats[key_index]
            stat.success_count += 1
            stat.last_model = model_name
            stat.last_error = ""

    def _record_failure(self, key_index: int, model_name: str, error_text: str) -> None:
        with self._lock:
            stat = self._stats[key_index]
            stat.failure_count += 1
            stat.last_model = model_name
            stat.last_error = error_text

    async def generate_content(
        self,
        *,
        model_names: Sequence[str],
        contents: Any,
        system_instruction: str | None = None,
        tools: list[types.Tool] | None = None,
        response_mime_type: str | None = None,
        response_schema: Any = None,
        thinking_config: types.ThinkingConfig | None = None,
    ) -> object:
        """Generate content with model and key fallbacks."""
        unique_models: list[str] = []
        for raw_model in model_names:
            model_name = str(raw_model or "").strip()
            if model_name and model_name not in unique_models:
                unique_models.append(model_name)
        if not unique_models:
            raise ValueError("At least one Gemini model name is required.")

        last_error: Exception | None = None
        for model_name in unique_models:
            for key_index in self._ordered_key_indices():
                self._record_request(key_index, model_name)
                config_kwargs: dict[str, Any] = {}
                if system_instruction is not None:
                    config_kwargs["system_instruction"] = system_instruction
                if tools is not None:
                    config_kwargs["tools"] = tools
                if response_mime_type is not None:
                    config_kwargs["response_mime_type"] = response_mime_type
                if response_schema is not None:
                    config_kwargs["response_schema"] = response_schema
                if thinking_config is not None:
                    config_kwargs["thinkingConfig"] = thinking_config
                config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

                try:
                    response = await self._clients[key_index].aio.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=config,
                    )
                    self._record_success(key_index, model_name)
                    return response
                except errors.APIError as exc:
                    status_code = getattr(exc, "status_code", None)
                    self._record_failure(
                        key_index,
                        model_name,
                        f"APIError:{status_code}",
                    )
                    last_error = exc
                    if status_code == 404:
                        LOGGER.warning(
                            "Gemini model %s is not available; trying the next fallback model.",
                            model_name,
                        )
                        break
                    if status_code in RETRYABLE_STATUS_CODES:
                        continue
                except Exception as exc:
                    self._record_failure(
                        key_index,
                        model_name,
                        type(exc).__name__,
                    )
                    last_error = exc
                    continue

        if last_error is not None:
            raise RuntimeError("Failed to get a response from Gemini.") from last_error
        raise RuntimeError("Gemini returned an empty response.")
