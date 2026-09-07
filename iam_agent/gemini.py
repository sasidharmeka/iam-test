"""Google Gemini structured-output adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from iam_agent.errors import DependencyError, ModelOutputError
from iam_agent.gemini_contracts import GeminiAnalysisPlan, GeminiDecomposition
from iam_agent.models import AnalysisPlan, QuestionDecomposition

if TYPE_CHECKING:
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
        wire_model = {
            AnalysisPlan: GeminiAnalysisPlan,
            QuestionDecomposition: GeminiDecomposition,
        }.get(response_model, response_model)
        if response_model is AnalysisPlan:
            system_instruction += (
                ' Encode each filter value in value_json, e.g. "\\"user12\\"" or "80". '
                "This is JSON data, never SQL. Return all listed fields; use [] for unused lists."
            )
        try:
            response = await self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=wire_model,
                    temperature=0.1,
                ),
            )
        except Exception as exc:
            status = getattr(exc, "code", None)
            if status == 400:
                raise DependencyError(
                    "gemini_request_rejected",
                    "Gemini rejected the structured analysis request. Check the configured model "
                    "and the request-schema details in Cloud Run logs.",
                ) from exc
            if status in {401, 403}:
                raise DependencyError("gemini_permission", "Gemini access was denied.") from exc
            if status in {429, 500, 502, 503, 504}:
                raise DependencyError(
                    "gemini_unavailable", "Gemini is temporarily unavailable or quota-limited."
                ) from exc
            raise
        try:
            parsed = (
                wire_model.model_validate(response.parsed)
                if response.parsed is not None
                else wire_model.model_validate_json(response.text or "")
            )
            if isinstance(parsed, (GeminiAnalysisPlan, GeminiDecomposition)):
                return parsed.to_application()
            return response_model.model_validate(parsed)
        except (ValueError, TypeError) as exc:
            raise ModelOutputError(
                f"Invalid {response_model.__name__} output; regenerate all required fields "
                "and use the supplied catalog and operation rules."
            ) from exc

    async def close(self) -> None:
        await self._client.aclose()
