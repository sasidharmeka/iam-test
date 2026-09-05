import pytest

from iam_agent.authentication import ChatAuthenticationError, GoogleChatRequestVerifier
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
