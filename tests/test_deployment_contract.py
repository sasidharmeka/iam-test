from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "deploy-agent.yml"


def test_manual_deployment_workflow_is_valid_yaml() -> None:
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"]["id-token"] == "write"
    assert "deploy" in workflow["jobs"]


def test_deployment_uses_keyless_wif_and_application_authentication() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "google-github-actions/auth@v3" in workflow
    assert "workload_identity_provider" in workflow
    assert "GCP_SA_KEY" not in workflow
    assert "--no-allow-unauthenticated" in workflow
    assert "--min=0" in workflow
    assert "--max=1" in workflow
    assert "--no-invoker-iam-check" in workflow
    assert "VERIFY_GOOGLE_CHAT_TOKEN=true" in workflow
    assert "GOOGLE_CHAT_AUDIENCE=${service_url}/google-chat/events" in workflow


def test_deployment_injects_data_identity_and_database_configuration() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    required_fragments = (
        "BIGQUERY_PROJECT",
        "BIGQUERY_DATASET",
        "BIGQUERY_ROWS_PER_TABLE",
        "GEMINI_MODEL",
        "CHAT_ANALYST_PRINCIPALS",
        "CHAT_APPROVER_PRINCIPALS",
        "WEB_GOOGLE_CLIENT_ID",
        "WEB_ANALYST_PRINCIPALS",
        "--add-cloudsql-instances",
        "--set-secrets=\"DATABASE_URL=${DATABASE_SECRET}:latest\"",
    )
    for fragment in required_fragments:
        assert fragment in workflow
