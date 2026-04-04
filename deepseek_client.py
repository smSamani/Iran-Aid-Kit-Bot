"""Lightweight DeepSeek-compatible client for Developer Mode."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx


LOGGER = logging.getLogger(__name__)
DEFAULT_PROXY_AUTHORIZATION = "Bearer local-proxy"


class DeepSeekDeveloperClient:
    """Call an OpenAI-compatible DeepSeek proxy endpoint."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 180.0) -> None:
        cleaned_base_url = base_url.strip().rstrip("/")
        if not cleaned_base_url:
            raise ValueError("DeepSeek base URL is required.")
        self._base_url = cleaned_base_url
        self._endpoint = f"{cleaned_base_url}/v1/chat/completions"
        self._timeout_seconds = max(float(timeout_seconds), 5.0)

    @property
    def base_url(self) -> str:
        """Return the configured API base URL."""
        return self._base_url

    async def generate_developer_response(
        self,
        *,
        model_name: str,
        system_instruction: str,
        prompt: str,
    ) -> object:
        """Send one Developer Mode request and return a text-like response object."""
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._endpoint,
                    json=payload,
                    headers={
                        "Authorization": DEFAULT_PROXY_AUTHORIZATION,
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "").strip()
            detail = body[:280] if body else exc.response.reason_phrase
            raise RuntimeError(
                f"DeepSeek API returned HTTP {exc.response.status_code}: {detail or 'unknown error'}"
            ) from exc
        except Exception as exc:
            LOGGER.exception("DeepSeek developer request failed")
            raise RuntimeError("Failed to reach the DeepSeek API endpoint.") from exc

        try:
            payload_data = response.json()
        except ValueError as exc:
            raise RuntimeError("DeepSeek API returned non-JSON output.") from exc

        choices = payload_data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("DeepSeek API returned no choices.")

        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("DeepSeek API returned an invalid message payload.")

        content = str(message.get("content") or "").strip()
        if not content:
            raise RuntimeError("DeepSeek API returned an empty response.")

        resolved_model = (
            str(payload_data.get("model") or "").strip()
            or str(model_name or "").strip()
        )
        return SimpleNamespace(
            text=content,
            model_version=resolved_model,
        )
