"""Gemini REST adapter (generateContent with JSON output).

STATUS: not verified against the live API in this repository. Request shape follows the
publicly documented v1beta REST format; re-verify field names, model availability, pricing
and data terms before enabling (see docs/STATUS.md). The model is set only via config.
"""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from typing import Any

import httpx
from pydantic import ValidationError

from fitcoach.ai.types import AIUnavailableError, MealDraft, ProviderResult

PROMPT_VERSION = "meal_extraction_v1"

_NUM = {"type": "NUMBER", "nullable": True}
_MEAL_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "quantity_text": {"type": "STRING", "nullable": True},
                    "energy_kcal": _NUM,
                    "protein_g": _NUM,
                    "fat_g": _NUM,
                    "carbs_g": _NUM,
                },
                "required": ["name"],
            },
        }
    },
    "required": ["items"],
}


def load_prompt(name: str) -> str:
    return resources.files("fitcoach.prompts").joinpath(f"{name}.txt").read_text("utf-8")


class GeminiProvider:
    name = "gemini"
    is_mock = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def parse_meal(self, text: str, language: str) -> ProviderResult[MealDraft]:
        body = {
            "systemInstruction": {"parts": [{"text": load_prompt(PROMPT_VERSION)}]},
            # User text is data, wrapped and never concatenated into instructions.
            "contents": [{"role": "user", "parts": [{"text": json.dumps({"meal_text": text})}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
                "responseSchema": _MEAL_SCHEMA,
            },
        }
        data = await self._post(body)
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            draft = MealDraft.model_validate(json.loads(raw))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise AIUnavailableError("invalid_output") from exc
        usage = data.get("usageMetadata") or {}
        output_tokens = (usage.get("candidatesTokenCount") or 0) + (
            usage.get("thoughtsTokenCount") or 0
        )
        return ProviderResult(
            draft,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=output_tokens or None,
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise AIUnavailableError("provider_error") from exc
            else:
                if response.status_code == 200:
                    result: dict[str, Any] = response.json()
                    return result
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise AIUnavailableError("provider_error")
                if attempt >= self._max_retries:
                    raise AIUnavailableError("provider_error")
            await asyncio.sleep(2**attempt)
        raise AIUnavailableError("provider_error")  # pragma: no cover
