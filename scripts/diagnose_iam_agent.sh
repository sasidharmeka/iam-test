#!/usr/bin/env bash
# Read-only checks. Usage: bash scripts/diagnose_iam_agent.sh [error-reference]
set -euo pipefail

IAM_DIAG_PROJECT="${GCP_PROJECT_ID:-rapid-stage-506406-t4}"
IAM_DIAG_REGION="${GCP_REGION:-us-central1}"
IAM_DIAG_SERVICE="${CLOUD_RUN_SERVICE:-iam-agent}"
IAM_DIAG_REFERENCE="${1:-}"
if [[ -n "${IAM_DIAG_REFERENCE}" && ! "${IAM_DIAG_REFERENCE}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo 'The reference must contain only letters, numbers, underscores, or hyphens.' >&2
  exit 2
fi

gcloud config list --format='text(core.account,core.project)'
gcloud run services describe "${IAM_DIAG_SERVICE}" \
  --project="${IAM_DIAG_PROJECT}" --region="${IAM_DIAG_REGION}" \
  --format='yaml(status.url,status.latestReadyRevisionName,spec.template.spec.serviceAccountName)'

IAM_DIAG_FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${IAM_DIAG_SERVICE}\""
if [[ -n "${IAM_DIAG_REFERENCE}" ]]; then
  IAM_DIAG_FILTER+=" AND SEARCH(\"${IAM_DIAG_REFERENCE}\")"
else
  IAM_DIAG_FILTER+=' AND (severity>=ERROR OR textPayload:"Traceback" OR textPayload:"IAM request failed" OR textPayload:"BigQuery")'
fi
gcloud logging read "${IAM_DIAG_FILTER}" --project="${IAM_DIAG_PROJECT}" \
  --freshness=7d --limit=60 --order=desc \
  --format='yaml(timestamp,resource.labels.revision_name,textPayload,jsonPayload)'
