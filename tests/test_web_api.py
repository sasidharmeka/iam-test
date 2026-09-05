from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from iam_agent.api import Container, create_app
from iam_agent.config import Settings


class FakeRepository:
    def create_schema(self) -> None:
        pass


class FakeGemini:
    async def close(self) -> None:
        pass


class FakeVerifier:
    def verify(self, authorization_header: str | None) -> dict[str, str]:
        assert authorization_header == "Bearer google-id-token"
        return {
            "email": "personal.user@gmail.com",
            "name": "Personal User",
            "picture": "",
        }


class FakeChatHandler:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events.append(event)
        return {"text": "handled"}


def _test_client() -> tuple[TestClient, FakeChatHandler]:
    chat = FakeChatHandler()
    settings = Settings(
        enable_web_ui=True,
        web_google_client_id="123-demo.apps.googleusercontent.com",
        _env_file=None,
    )
    container = Container(
        settings=settings,
        repository=FakeRepository(),  # type: ignore[arg-type]
        gemini=FakeGemini(),  # type: ignore[arg-type]
        verifier=FakeVerifier(),  # type: ignore[arg-type]
        web_verifier=FakeVerifier(),  # type: ignore[arg-type]
        chat=chat,  # type: ignore[arg-type]
    )
    return TestClient(create_app(container)), chat


def test_web_ui_contains_google_client_id() -> None:
    client, _ = _test_client()

    with client:
        response = client.get("/")

    assert response.status_code == 200
    assert "123-demo.apps.googleusercontent.com" in response.text
    assert "https://accounts.google.com/gsi/client" in response.text


def test_web_message_uses_verified_gmail_identity() -> None:
    client, chat = _test_client()

    with client:
        response = client.post(
            "/web/messages",
            headers={"Authorization": "Bearer google-id-token"},
            json={"text": "investigate user1"},
        )

    assert response.json() == {"text": "handled"}
    event = chat.events[0]
    assert event["type"] == "MESSAGE"
    assert event["user"]["email"] == "personal.user@gmail.com"
    assert event["message"]["text"] == "investigate user1"


def test_web_decision_uses_verified_gmail_identity() -> None:
    client, chat = _test_client()

    with client:
        response = client.post(
            "/web/actions/action-123/decision",
            headers={"Authorization": "Bearer google-id-token"},
            json={"approve": True},
        )

    assert response.json() == {"text": "handled"}
    event = chat.events[0]
    assert event["type"] == "CARD_CLICKED"
    assert event["user"]["email"] == "personal.user@gmail.com"
    assert event["common"]["invokedFunction"] == "approve_action"
    assert event["common"]["parameters"]["action_id"] == "action-123"
