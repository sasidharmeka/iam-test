from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "deploy-agent.yml"
SETUP_PATH = REPOSITORY_ROOT / "setup_iam_agent.sh"


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


def test_deployment_injects_read_only_query_limits_without_database() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    required_fragments = (
        "BIGQUERY_PROJECT",
        "BIGQUERY_DATASET",
        "QUERY_MAX_ROWS",
        "QUERY_MAX_BYTES_BILLED",
        "QUERY_MAX_STEPS",
        "GEMINI_MODEL",
        "CHAT_ANALYST_PRINCIPALS",
        "WEB_GOOGLE_CLIENT_ID",
        "WEB_ANALYST_PRINCIPALS",
    )
    for fragment in required_fragments:
        assert fragment in workflow
    assert "QUERY_MAX_LOOKBACK_DAYS" not in workflow

    forbidden_fragments = (
        "CLOUD_SQL_INSTANCE",
        "DATABASE_SECRET",
        "DATABASE_URL",
        "--add-cloudsql-instances",
        "--set-secrets",
        "APPROVER_PRINCIPALS",
    )
    for fragment in forbidden_fragments:
        assert fragment not in workflow

    # The workflow authenticates as the deployer, which intentionally has no
    # permission to inspect IAM data. BigQuery access belongs to the runtime SA.
    assert 'show "${BIGQUERY_PROJECT}:${BIGQUERY_DATASET}"' not in workflow


def test_setup_is_idempotent_and_has_no_database_or_destructive_commands() -> None:
    script = SETUP_PATH.read_text(encoding="utf-8")

    assert "gcloud builds get-default-service-account" in script
    assert "roles/run.builder" in script
    assert "roles/iam.workloadIdentityUser" in script
    assert "roles/bigquery.dataViewer" in script
    assert "roles/aiplatform.user" in script
    assert "sql instances create" not in script
    assert "sql instances delete" not in script
    assert "secrets create" not in script
    assert "rm -rf" not in script
