from __future__ import annotations

from fastapi.testclient import TestClient

from iam_agent.api import Container, create_app
from iam_agent.config import Settings


class FakeGemini:
    async def close(self) -> None:
        return None


class FakeChatVerifier:
    def verify(self, authorization: str | None) -> None:
        return None


class FakeWebVerifier:
    def verify(self, authorization: str | None) -> dict[str, str]:
        return {"email": "analyst@example.com", "name": "Analyst"}


class FakeMCP:
    tool_names = ("list_iam_domains", "describe_iam_domains", "run_iam_query")

    async def initialize(self) -> None:
        return None


class FakeChat:
    async def handle(self, event: dict) -> dict[str, str]:
        assert event["user"]["email"] == "analyst@example.com"
        return {"text": f"Asked: {event['message']['text']}"}


def test_health_and_authenticated_web_chat_are_read_only() -> None:
    settings = Settings(
        enable_web_ui=True,
        web_google_client_id="client.apps.googleusercontent.com",
        _env_file=None,
    )
    container = Container(
        settings=settings,
        gemini=FakeGemini(),
        verifier=FakeChatVerifier(),
        web_verifier=FakeWebVerifier(),
        mcp_client=FakeMCP(),
        chat=FakeChat(),
    )

    with TestClient(create_app(container)) as client:
        health = client.get("/healthz")
        reply = client.post(
            "/web/messages",
            headers={"Authorization": "Bearer demo"},
            json={"text": "Who is riskiest?"},
        )
        removed_endpoint = client.post("/google-chat/decisions/action-1", json={})

    assert health.json()["mode"] == "read-only"
    assert health.json()["mcp_tools"] == list(FakeMCP.tool_names)
    assert reply.json() == {"text": "Asked: Who is riskiest?"}
    assert removed_endpoint.status_code == 404
