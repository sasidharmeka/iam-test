import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import _transformers, types
from pydantic import ValidationError

from iam_agent.errors import DependencyError, ModelOutputError
from iam_agent.gemini import GeminiClient
from iam_agent.gemini_contracts import GeminiAnalysisPlan, GeminiDecomposition
from iam_agent.models import AnalysisPlan


def wire_plan(value='"user12"'):
    return {"objective": "Inspect user12", "steps": [{
        "step_id": "risk", "purpose": "Inspect risk factors", "concept": "risk_scores",
        "operation": "records", "fields": [], "metrics": [], "dimensions": [],
        "filters": [{"field": "user", "operator": "equals", "value_json": value}],
        "time_range_days": None, "order_by": [], "limit": 5,
    }]}


def test_simple_wire_contract_passes_real_google_sdk_schema_conversion():
    for contract in (GeminiAnalysisPlan, GeminiDecomposition):
        schema = _transformers.t_schema(None, contract)
        assert schema.properties
    # Unlike the internal scalar/date/array union, filter generation has one simple string type.
    assert GeminiAnalysisPlan.model_json_schema()["$defs"]["GeminiFilter"]["properties"]["value_json"]["type"] == "string"


def test_decoding_filter_json_retains_strict_application_validation():
    plan = GeminiAnalysisPlan.model_validate(wire_plan()).to_application()
    assert plan.steps[0].filters[0].value == "user12"
    with pytest.raises(ValidationError):
        GeminiAnalysisPlan.model_validate(wire_plan('{"sql": "SELECT *"}')).to_application()
    with pytest.raises(ValidationError):
        draft = wire_plan()
        draft["steps"][0]["metrics"] = ["count"]
        GeminiAnalysisPlan.model_validate(draft).to_application()


@pytest.mark.asyncio
async def test_adapter_uses_wire_schema_and_validates_response():
    client = GeminiClient.__new__(GeminiClient)
    client._types = types
    client._model = "gemini-2.5-flash"
    generate = AsyncMock(return_value=SimpleNamespace(parsed=None, text=json.dumps(wire_plan())))
    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    result = await client.generate_structured(
        system_instruction="Plan", prompt="Inspect user12", response_model=AnalysisPlan,
    )
    assert result.steps[0].filters[0].value == "user12"
    assert generate.call_args.kwargs["config"].response_schema is GeminiAnalysisPlan
    generate.return_value = SimpleNamespace(parsed=None, text="")
    with pytest.raises(ModelOutputError):
        await client.generate_structured(system_instruction="Plan", prompt="Q", response_model=AnalysisPlan)


@pytest.mark.asyncio
async def test_model_permission_error_is_not_misreported_as_a_domain_failure():
    from google.genai.errors import ClientError

    client = GeminiClient.__new__(GeminiClient)
    client._types = types
    client._model = "gemini-2.5-flash"
    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock(
        side_effect=ClientError(403, {"error": {"message": "Forbidden", "code": 403}})
    )))
    with pytest.raises(DependencyError) as failure:
        await client.generate_structured(system_instruction="Plan", prompt="Q", response_model=AnalysisPlan)
    assert failure.value.code == "gemini_permission"
