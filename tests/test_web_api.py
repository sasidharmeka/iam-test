from __future__ import annotations

from fastapi.testclient import TestClient

from iam_agent.api import Container, create_app
from iam_agent.config import Settings
from iam_agent.models import ReportFormat
from iam_agent.reporting import ReportStore


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
    async def handle(self, event: dict, *, include_reports: bool = False) -> dict[str, str]:
        assert event["user"]["email"] == "analyst@example.com"
        assert include_reports is True
        return {"text": f"Asked: {event['message']['text']}"}


def test_health_and_authenticated_web_chat_are_read_only() -> None:
    settings = Settings(
        enable_web_ui=True,
        web_google_client_id="client.apps.googleusercontent.com",
        _env_file=None,
    )
    report_store = ReportStore()
    container = Container(
        settings=settings,
        gemini=FakeGemini(),
        verifier=FakeChatVerifier(),
        web_verifier=FakeWebVerifier(),
        mcp_client=FakeMCP(),
        chat=FakeChat(),
        report_store=report_store,
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


def test_report_download_requires_same_authenticated_owner() -> None:
    settings = Settings(
        enable_web_ui=True,
        web_google_client_id="client.apps.googleusercontent.com",
        _env_file=None,
    )
    report_store = ReportStore()
    container = Container(
        settings=settings,
        gemini=FakeGemini(),
        verifier=FakeChatVerifier(),
        web_verifier=FakeWebVerifier(),
        mcp_client=FakeMCP(),
        chat=FakeChat(),
        report_store=report_store,
    )

    with TestClient(create_app(container)) as client:
        import asyncio

        descriptor = asyncio.run(
            report_store.add(
                owner="analyst@example.com",
                report_format=ReportFormat.CSV,
                filename="iam-report.csv",
                content=b"user,risk\nuser12,91\n",
            )
        )
        response = client.get(
            descriptor.download_url,
            headers={"Authorization": "Bearer demo"},
        )

    assert response.status_code == 200
    assert response.content.startswith(b"user,risk")
    assert "iam-report.csv" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
