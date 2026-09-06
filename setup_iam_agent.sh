#!/usr/bin/env bash

# Idempotent Google Cloud setup for sasidharmeka/multi-agent-iam-demo.
# Run in Google Cloud Shell as a project Owner. The script creates no database,
# stores no password, deletes nothing, and is safe to rerun.

set -Eeuo pipefail

trap 'status=$?; printf "\nERROR: setup failed at line %s (exit code %s).\n" "${LINENO}" "${status}" >&2; printf "Review ~/setup_iam_agent.log and the preceding Google Cloud error.\n" >&2; exit "${status}"' ERR

PROJECT_ID="rapid-stage-506406-t4"
REGION="us-central1"
GITHUB_OWNER="sasidharmeka"
AGENT_REPOSITORY="sasidharmeka/multi-agent-iam-demo"
PIPELINE_REPOSITORY="sasidharmeka/IAM_NEW_1"
WIF_POOL_ID="github-actions"
WIF_PROVIDER_ID="iam-repo"
DEPLOY_SA_NAME="iam-agent-deploy"
RUNTIME_SA_NAME="iam-agent-runtime"

info() {
  printf '\n===== %s =====\n' "$1"
}

die() {
  printf '\nERROR: %s\n' "$1" >&2
  exit 1
}

command -v gcloud >/dev/null 2>&1 || die "gcloud is required. Run this in Google Cloud Shell."
command -v python3 >/dev/null 2>&1 || die "python3 is required."

ACTIVE_ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
[[ -n "${ACTIVE_ACCOUNT}" && "${ACTIVE_ACCOUNT}" != "(unset)" ]] \
  || die "No active Google account. Run: gcloud auth login"

info "ACCOUNT AND PROJECT"
printf 'Active account: %s\n' "${ACTIVE_ACCOUNT}"
printf 'Target project: %s\n' "${PROJECT_ID}"
gcloud projects describe "${PROJECT_ID}" \
  --format='table(projectId,projectNumber,lifecycleState)'
gcloud config set project "${PROJECT_ID}" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
[[ -n "${PROJECT_NUMBER}" ]] || die "Could not determine the project number."

DEPLOY_SA_EMAIL="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA_EMAIL="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/providers/${WIF_PROVIDER_ID}"

info "ENABLE REQUIRED APIS"
gcloud services enable \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  bigquery.googleapis.com \
  chat.googleapis.com \
  cloudbuild.googleapis.com \
  cloudresourcemanager.googleapis.com \
  compute.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  sts.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet

info "SERVICE ACCOUNTS"
if gcloud iam service-accounts describe "${DEPLOY_SA_EMAIL}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  printf 'Using existing deployer: %s\n' "${DEPLOY_SA_EMAIL}"
else
  gcloud iam service-accounts create "${DEPLOY_SA_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="IAM agent GitHub deployer"
fi

if gcloud iam service-accounts describe "${RUNTIME_SA_EMAIL}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  printf 'Using existing runtime account: %s\n' "${RUNTIME_SA_EMAIL}"
else
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="Read-only IAM agent runtime"
fi

info "RUNTIME IAM ROLES"
runtime_roles=(
  roles/aiplatform.user
  roles/bigquery.dataViewer
  roles/bigquery.jobUser
)
for role in "${runtime_roles[@]}"; do
  printf 'Granting %s to %s\n' "${role}" "${RUNTIME_SA_EMAIL}"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet \
    --format=none
done

info "DEPLOYER IAM ROLES"
deployer_roles=(
  roles/run.admin
  roles/run.sourceDeveloper
  roles/serviceusage.serviceUsageConsumer
)
for role in "${deployer_roles[@]}"; do
  printf 'Granting %s to %s\n' "${role}" "${DEPLOY_SA_EMAIL}"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet \
    --format=none
done

gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --condition=None \
  --quiet \
  --format=none

info "CLOUD BUILD SERVICE ACCOUNT"
BUILD_SA_RESOURCE="$(gcloud builds get-default-service-account \
  --project="${PROJECT_ID}" 2>/dev/null || true)"
BUILD_SA_EMAIL="${BUILD_SA_RESOURCE##*/}"
if [[ -z "${BUILD_SA_EMAIL}" || "${BUILD_SA_EMAIL}" != *@* ]]; then
  BUILD_SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

build_sa_ready=false
for attempt in {1..12}; do
  if gcloud iam service-accounts describe "${BUILD_SA_EMAIL}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
    build_sa_ready=true
    break
  fi
  printf 'Waiting for build account %s (%s/12)...\n' "${BUILD_SA_EMAIL}" "${attempt}"
  sleep 5
done
[[ "${build_sa_ready}" == "true" ]] \
  || die "Build account ${BUILD_SA_EMAIL} is unavailable."

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${BUILD_SA_EMAIL}" \
  --role="roles/run.builder" \
  --condition=None \
  --quiet \
  --format=none

# Source deployment submits a Cloud Build job. The deployer must be permitted
# to act as whichever service account Google Cloud selected for that build.
gcloud iam service-accounts add-iam-policy-binding "${BUILD_SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --condition=None \
  --quiet \
  --format=none

info "WORKLOAD IDENTITY FEDERATION"
if gcloud iam workload-identity-pools describe "${WIF_POOL_ID}" \
  --location=global \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  printf 'Using existing WIF pool: %s\n' "${WIF_POOL_ID}"
else
  gcloud iam workload-identity-pools create "${WIF_POOL_ID}" \
    --location=global \
    --project="${PROJECT_ID}" \
    --display-name="GitHub Actions"
fi

if gcloud iam workload-identity-pools providers describe "${WIF_PROVIDER_ID}" \
  --workload-identity-pool="${WIF_POOL_ID}" \
  --location=global \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  PROVIDER_JSON="$(gcloud iam workload-identity-pools providers describe \
    "${WIF_PROVIDER_ID}" \
    --workload-identity-pool="${WIF_POOL_ID}" \
    --location=global \
    --project="${PROJECT_ID}" \
    --format=json)"

  ATTRIBUTE_REPOSITORY_MAPPING="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("attributeMapping", {}).get("attribute.repository", ""))' <<<"${PROVIDER_JSON}")"
  [[ "${ATTRIBUTE_REPOSITORY_MAPPING}" == "assertion.repository" ]] \
    || die "Existing WIF provider does not map attribute.repository to assertion.repository."

  PROVIDER_CONDITION="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("attributeCondition", ""))' <<<"${PROVIDER_JSON}")"
  if [[ "${PROVIDER_CONDITION}" == *"${AGENT_REPOSITORY}"* ]] \
    || { [[ "${PROVIDER_CONDITION}" == *"repository_owner"* ]] \
      && [[ "${PROVIDER_CONDITION}" == *"${GITHUB_OWNER}"* ]]; }; then
    printf 'Existing provider condition already permits %s.\n' "${AGENT_REPOSITORY}"
  elif [[ -z "${PROVIDER_CONDITION}" || "${PROVIDER_CONDITION}" == *"${PIPELINE_REPOSITORY}"* ]]; then
    printf 'Restricting the provider condition to the two IAM repositories.\n'
    UPDATED_CONDITION="assertion.repository == '${PIPELINE_REPOSITORY}' || assertion.repository == '${AGENT_REPOSITORY}'"
    gcloud iam workload-identity-pools providers update-oidc "${WIF_PROVIDER_ID}" \
      --workload-identity-pool="${WIF_POOL_ID}" \
      --location=global \
      --project="${PROJECT_ID}" \
      --attribute-condition="${UPDATED_CONDITION}"
  else
    printf 'Current provider condition: %s\n' "${PROVIDER_CONDITION}" >&2
    die "The existing WIF condition is unfamiliar and was not changed."
  fi
else
  PROVIDER_CONDITION="assertion.repository == '${PIPELINE_REPOSITORY}' || assertion.repository == '${AGENT_REPOSITORY}'"
  gcloud iam workload-identity-pools providers create-oidc "${WIF_PROVIDER_ID}" \
    --workload-identity-pool="${WIF_POOL_ID}" \
    --location=global \
    --project="${PROJECT_ID}" \
    --display-name="IAM GitHub repositories" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="${PROVIDER_CONDITION}"
fi

WIF_PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/attribute.repository/${AGENT_REPOSITORY}"
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --member="${WIF_PRINCIPAL}" \
  --role="roles/iam.workloadIdentityUser" \
  --condition=None \
  --quiet \
  --format=none

info "VERIFY BIGQUERY PIPELINE OUTPUTS"
if bq --project_id="${PROJECT_ID}" show "${PROJECT_ID}:iam_data_dev" >/dev/null 2>&1; then
  TABLE_COUNT="$(bq --project_id="${PROJECT_ID}" ls --format=json \
    "${PROJECT_ID}:iam_data_dev" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
  printf 'iam_data_dev is present with %s table(s). The agent catalog expects 22 outputs.\n' \
    "${TABLE_COUNT}"
else
  printf 'WARNING: iam_data_dev is not present yet. Run the IAM_NEW_1 pipeline before testing.\n'
fi

info "SETUP COMPLETE"
cat <<VALUES
Add these as GitHub Environment variables under Settings > Environments > dev:

GCP_PROJECT_ID=${PROJECT_ID}
GCP_REGION=${REGION}
WIF_PROVIDER=${WIF_PROVIDER}
DEPLOY_SERVICE_ACCOUNT=${DEPLOY_SA_EMAIL}
RUNTIME_SERVICE_ACCOUNT=${RUNTIME_SA_EMAIL}
CLOUD_RUN_SERVICE=iam-agent
GOOGLE_CLOUD_LOCATION=${REGION}
GEMINI_MODEL=gemini-2.5-flash
BIGQUERY_PROJECT=${PROJECT_ID}
BIGQUERY_DATASET=iam_data_dev
QUERY_MAX_ROWS=500
QUERY_MAX_BYTES_BILLED=500000000
QUERY_MAX_LOOKBACK_DAYS=90
QUERY_MAX_STEPS=6
WEB_GOOGLE_CLIENT_ID=YOUR_EXISTING_WEB_CLIENT_ID
WEB_ANALYST_PRINCIPALS=YOUR_GOOGLE_ACCOUNT_EMAIL
CHAT_ANALYST_PRINCIPALS=
QUERY_DEFAULT_SCOPE=/company

Build service account: ${BUILD_SA_EMAIL}

There are no DATABASE_SECRET, CLOUD_SQL_INSTANCE, or approver variables in this
read-only version. Next run Actions > Deploy read-only IAM agent > Run workflow.
VALUES
