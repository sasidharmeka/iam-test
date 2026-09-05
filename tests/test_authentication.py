from unittest.mock import patch

import pytest

from iam_agent.authentication import (
    ChatAuthenticationError,
    GoogleChatRequestVerifier,
    GoogleWebIdentityVerifier,
    WebAuthenticationError,
)
from iam_agent.config import Settings


def test_cloud_run_managed_auth_mode_does_not_reverify_token() -> None:
    verifier = GoogleChatRequestVerifier(Settings(verify_google_chat_token=False, _env_file=None))
    verifier.verify(None)


def test_in_app_verification_requires_bearer_token() -> None:
    verifier = GoogleChatRequestVerifier(
        Settings(
            verify_google_chat_token=True,
            google_chat_audience="https://iam-agent.example.run.app",
            _env_file=None,
        )
    )
    with pytest.raises(ChatAuthenticationError):
        verifier.verify(None)


def test_web_verification_requires_bearer_token() -> None:
    verifier = GoogleWebIdentityVerifier(
        Settings(
            enable_web_ui=True,
            web_google_client_id="123-demo.apps.googleusercontent.com",
            _env_file=None,
        )
    )

    with pytest.raises(WebAuthenticationError):
        verifier.verify(None)


def test_web_verification_accepts_verified_google_account() -> None:
    verifier = GoogleWebIdentityVerifier(
        Settings(
            enable_web_ui=True,
            web_google_client_id="123-demo.apps.googleusercontent.com",
            _env_file=None,
        )
    )
    claims = {
        "email": "personal.user@gmail.com",
        "email_verified": True,
        "name": "Personal User",
        "picture": "https://example.test/avatar.png",
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims) as verify:
        identity = verifier.verify("Bearer google-id-token")

    assert identity["email"] == "personal.user@gmail.com"
    assert identity["name"] == "Personal User"
    assert verify.call_args.args[0] == "google-id-token"
    assert verify.call_args.args[1].__class__.__name__ == "Request"
    assert verify.call_args.args[2] == "123-demo.apps.googleusercontent.com"


def test_web_verification_rejects_unverified_email() -> None:
    verifier = GoogleWebIdentityVerifier(
        Settings(
            enable_web_ui=True,
            web_google_client_id="123-demo.apps.googleusercontent.com",
            _env_file=None,
        )
    )
    claims = {"email": "personal.user@gmail.com", "email_verified": False}

    with (
        patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims),
        pytest.raises(WebAuthenticationError),
    ):
        verifier.verify("Bearer google-id-token")
