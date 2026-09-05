"""Verification of bearer tokens sent to the Google Chat HTTP endpoint."""

from __future__ import annotations

from iam_agent.config import Settings


class ChatAuthenticationError(PermissionError):
    pass


class WebAuthenticationError(PermissionError):
    pass


class GoogleChatRequestVerifier:
    """Verifies Chat's OIDC token when enabled for a publicly reachable endpoint."""

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


class GoogleWebIdentityVerifier:
    """Verifies Google Identity Services ID tokens presented by the web UI."""

    def __init__(self, settings: Settings):
        self.enabled = settings.enable_web_ui
        self.client_id = settings.web_google_client_id

    def verify(self, authorization_header: str | None) -> dict[str, str]:
        if not self.enabled:
            raise WebAuthenticationError("The web UI is disabled")
        if not self.client_id:
            raise WebAuthenticationError("WEB_GOOGLE_CLIENT_ID is required")
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise WebAuthenticationError("Missing Google bearer token")

        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(
                authorization_header.removeprefix("Bearer ").strip(),
                Request(),
                self.client_id,
            )
        except Exception as exc:
            raise WebAuthenticationError("Google ID token verification failed") from exc

        email = str(claims.get("email", "")).strip()
        email_verified = claims.get("email_verified")
        if not email or email_verified not in {True, "true"}:
            raise WebAuthenticationError("Google account email is not verified")
        return {
            "email": email,
            "name": str(claims.get("name", "")),
            "picture": str(claims.get("picture", "")),
        }
