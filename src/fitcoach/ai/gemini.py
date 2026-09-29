"""Gemini REST adapter (v1beta generateContent, JSON Schema structured output).

Live-verified on 2026-09-29 with gemini-3.5-flash-lite for text extraction and
`responseJsonSchema` (see docs/STATUS.md). Model names come from configuration only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
from importlib import resources
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from fitcoach.ai.types import (
    ActivitySchemaDraft,
    AIUnavailableError,
    FoodParse,
    ProviderResult,
    WorkoutParse,
)

PROMPTS = {
    "food_text": "food_text_v1",
    "food_image": "food_image_v1",
    "food_voice": "food_voice_v1",
    "workout_text": "workout_text_v1",
    "activity_schema": "activity_schema_v1",
}
MAX_RESPONSE_BYTES = 256_000


def load_prompt(name: str) -> str:
    return resources.files("fitcoach.prompts").joinpath(f"{name}.txt").read_text("utf-8")


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


class GeminiProvider:
    name = "gemini"
    is_mock = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        media_model: str | None = None,
        base_url: str,
        timeout: float,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.media_model = media_model or model
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    # --- tasks -------------------------------------------------------------------

    async def parse_food_text(self, text: str, language: str) -> ProviderResult[FoodParse]:
        return await self._generate(
            "food_text", self.model, FoodParse, [_data_part({"language": language, "text": text})]
        )

    async def analyze_food_image(
        self, image: bytes, mime: str, caption: str | None, language: str
    ) -> ProviderResult[FoodParse]:
        parts = [_inline(image, mime), _data_part({"language": language, "caption": caption})]
        return await self._generate("food_image", self.media_model, FoodParse, parts)

    async def parse_food_voice(
        self, audio: bytes, mime: str, language: str
    ) -> ProviderResult[FoodParse]:
        parts = [_inline(audio, mime), _data_part({"language": language})]
        return await self._generate("food_voice", self.media_model, FoodParse, parts)

    async def parse_workout_text(self, text: str, language: str) -> ProviderResult[WorkoutParse]:
        return await self._generate(
            "workout_text",
            self.model,
            WorkoutParse,
            [_data_part({"language": language, "text": text})],
        )

    async def build_activity_draft(
        self, text: str, language: str
    ) -> ProviderResult[ActivitySchemaDraft]:
        return await self._generate(
            "activity_schema",
            self.model,
            ActivitySchemaDraft,
            [_data_part({"language": language, "text": text})],
        )

    async def check(self) -> list[str]:
        missing = []
        for model in sorted({self.model, self.media_model}):
            try:
                response = await self._client.get(
                    f"{self._base}/models/{model}", headers={"x-goog-api-key": self._api_key}
                )
            except httpx.HTTPError as exc:
                raise AIUnavailableError("provider_unavailable", billable=False) from exc
            if response.status_code == 404:
                missing.append(model)
            elif response.status_code in (401, 403):
                raise AIUnavailableError("provider_auth", billable=False)
            elif response.status_code != 200:
                raise AIUnavailableError("provider_unavailable", billable=False)
        return missing

    # --- transport ---------------------------------------------------------------

    async def _generate[T: BaseModel](
        self, task: str, model: str, out: type[T], parts: list[dict[str, Any]]
    ) -> ProviderResult[T]:
        body = {
            "systemInstruction": {"parts": [{"text": load_prompt(PROMPTS[task])}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
                "responseJsonSchema": _schema(out),
            },
        }
        data = await self._post(model, body)
        try:
            candidate = data["candidates"][0]
            raw = "".join(p.get("text", "") for p in candidate["content"]["parts"])
            value = out.model_validate(json.loads(raw))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise AIUnavailableError("invalid_output") from exc
        usage = data.get("usageMetadata") or {}
        output_tokens = (usage.get("candidatesTokenCount") or 0) + (
            usage.get("thoughtsTokenCount") or 0
        )
        return ProviderResult(
            value,
            provider=self.name,
            model=model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=output_tokens or None,
        )

    async def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        for attempt in range(self._max_retries + 1):
            last_attempt = attempt >= self._max_retries
            try:
                response = await self._client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                # The request may have been processed: treat as billable.
                if last_attempt:
                    raise AIUnavailableError("provider_timeout") from exc
            except httpx.HTTPError as exc:
                if last_attempt:
                    raise AIUnavailableError("provider_unavailable", billable=False) from exc
            else:
                if response.status_code == 200:
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        raise AIUnavailableError("invalid_output")
                    result: dict[str, Any] = response.json()
                    return result
                if response.status_code == 404:
                    raise AIUnavailableError("model_unavailable", billable=False)
                if response.status_code in (401, 403):
                    raise AIUnavailableError("provider_auth", billable=False)
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise AIUnavailableError("provider_error", billable=False)
                if last_attempt:
                    billable = response.status_code != 429
                    raise AIUnavailableError("provider_error", billable=billable)
            await asyncio.sleep(min(8.0, 2**attempt) + random.random() / 2)  # noqa: S311
        raise AIUnavailableError("provider_error")  # pragma: no cover


def _data_part(payload: dict[str, Any]) -> dict[str, Any]:
    """User-provided content is passed as JSON data, never concatenated into instructions."""
    return {"text": json.dumps({"untrusted_user_input": payload}, ensure_ascii=False)}


def _inline(blob: bytes, mime: str) -> dict[str, Any]:
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(blob).decode("ascii")}}
