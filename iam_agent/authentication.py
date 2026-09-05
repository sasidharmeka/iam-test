"""Verification of bearer tokens sent to the Google Chat HTTP endpoint."""

from __future__ import annotations

from iam_agent.config import Settings


class ChatAuthenticationError(PermissionError):
    pass


class GoogleChatRequestVerifier:
    """Optionally verifies Chat's OIDC token in-app; Cloud Run IAM is the default."""

    def __init__(self, settings: Settings):
        self.enabled = settings.verify_google_chat_token
        self.audience = settings.google_chat_audience
        self.expected_service_account = settings.google_chat_service_account

    def verify(self, authorization_header: str | None) -> None:
        if not self.enabled:
            return
        if not self.audience:
            raise ChatAuthenticationError("GOOGLE_CHAT_AUDIENCE is required")
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise ChatAuthenticationError("Missing Google Chat bearer token")

        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(
                authorization_header.removeprefix("Bearer ").strip(),
                Request(),
                self.audience,
            )
        except Exception as exc:
            raise ChatAuthenticationError("Google Chat token verification failed") from exc
        if claims.get("email") != self.expected_service_account:
            raise ChatAuthenticationError("Token was not issued to Google Chat")
