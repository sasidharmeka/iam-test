"""Google Gemini structured-output adapter."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from iam_agent.config import Settings

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class GeminiClient:
    """Runs Gemini on Google Cloud with Application Default Credentials."""

    def __init__(self, settings: Settings):
        from google import genai
        from google.genai import types

        self._types = types
        self._model = settings.gemini_model
        self._client = genai.Client(
            enterprise=True,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            http_options=types.HttpOptions(api_version="v1"),
        ).aio

    async def generate_structured(
        self,
        *,
        system_instruction: str,
        prompt: str,
        response_model: type[OutputModel],
    ) -> OutputModel:
        response = await self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=self._types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=response_model,
                temperature=0.1,
            ),
        )
        if response.parsed is not None:
            return response_model.model_validate(response.parsed)
        return response_model.model_validate_json(response.text)

    async def close(self) -> None:
        await self._client.aclose()
