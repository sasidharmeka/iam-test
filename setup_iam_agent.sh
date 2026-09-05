#!/usr/bin/env bash

# One-time Google Cloud setup for sasidharmeka/multi-agent-iam-demo.
#
# This script creates the prerequisites consumed by
# .github/workflows/deploy-agent.yml. It is safe to rerun: existing resources
# are reused. It never deletes resources and never prints database credentials.

set -Eeuo pipefail
umask 077

trap 'status=$?; printf "\nERROR: setup failed at line %s (exit code %s).\n" "${LINENO}" "${status}" >&2; printf "Review ~/setup_iam_agent.log for the preceding Google Cloud error.\n" >&2; exit "${status}"' ERR

PROJECT_ID="rapid-stage-506406-t4"
REGION="us-central1"

GITHUB_OWNER="sasidharmeka"
GITHUB_REPOSITORY="sasidharmeka/multi-agent-iam-demo"
PIPELINE_REPOSITORY="sasidharmeka/IAM_NEW_1"

WIF_POOL_ID="github-actions"
WIF_PROVIDER_ID="iam-repo"

DEPLOY_SA_NAME="iam-agent-deploy"
RUNTIME_SA_NAME="iam-agent-runtime"

SQL_INSTANCE="iam-agent-db"
SQL_DATABASE="iam_agent"
SQL_USER="iam_agent_app"
DATABASE_SECRET="IAM_AGENT_DATABASE_URL"

info() {
  printf '\n===== %s =====\n' "$1"
}

die() {
  printf '\nERROR: %s\n' "$1" >&2
  exit 1
}

command -v gcloud >/dev/null 2>&1 || die "gcloud was not found. Run this script in Google Cloud Shell."
command -v python3 >/dev/null 2>&1 || die "python3 was not found."

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
BUILD_SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/providers/${WIF_PROVIDER_ID}"

info "ENABLE GOOGLE CLOUD APIS"
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
  secretmanager.googleapis.com \
  sqladmin.googleapis.com \
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
    --display-name="IAM agent Cloud Run runtime"
fi

info "RUNTIME IAM ROLES"
runtime_roles=(
  roles/aiplatform.user
  roles/bigquery.dataViewer
  roles/bigquery.jobUser
  roles/cloudsql.client
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
  roles/cloudsql.viewer
  roles/run.admin
  roles/run.sourceDeveloper
  roles/secretmanager.viewer
  roles/serviceusage.serviceUsageAdmin
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
build_sa_ready=false
for attempt in {1..18}; do
  if gcloud iam service-accounts describe "${BUILD_SA_EMAIL}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
    build_sa_ready=true
    break
  fi
  printf 'Waiting for the Compute Engine default service account (%s/18)...\n' "${attempt}"
  sleep 10
done
[[ "${build_sa_ready}" == "true" ]] \
  || die "Default build account ${BUILD_SA_EMAIL} was not created after enabling Compute Engine."

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${BUILD_SA_EMAIL}" \
  --role="roles/run.builder" \
  --condition=None \
  --quiet \
  --format=none

# A source deployment submits a Cloud Build job. The submitting deployer must
# be allowed to act as the service account selected to execute that build.
gcloud iam service-accounts add-iam-policy-binding "${BUILD_SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --condition=None \
  --quiet \
  --format=none

info "WORKLOAD IDENTITY FEDERATION"
gcloud iam workload-identity-pools describe "${WIF_POOL_ID}" \
  --location=global \
  --project="${PROJECT_ID}" >/dev/null \
  || die "WIF pool '${WIF_POOL_ID}' does not exist. The IAM_NEW_1 WIF setup must be completed first."

PROVIDER_JSON="$(gcloud iam workload-identity-pools providers describe "${WIF_PROVIDER_ID}" \
  --workload-identity-pool="${WIF_POOL_ID}" \
  --location=global \
  --project="${PROJECT_ID}" \
  --format=json)" \
  || die "WIF provider '${WIF_PROVIDER_ID}' does not exist."

ATTRIBUTE_REPOSITORY_MAPPING="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("attributeMapping", {}).get("attribute.repository", ""))' <<<"${PROVIDER_JSON}")"
[[ -n "${ATTRIBUTE_REPOSITORY_MAPPING}" ]] \
  || die "The WIF provider does not map attribute.repository. Do not change it blindly; send the provider JSON for review."

PROVIDER_CONDITION="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("attributeCondition", ""))' <<<"${PROVIDER_JSON}")"
if [[ "${PROVIDER_CONDITION}" == *"${GITHUB_REPOSITORY}"* ]] \
  || { [[ "${PROVIDER_CONDITION}" == *"repository_owner"* ]] \
    && [[ "${PROVIDER_CONDITION}" == *"${GITHUB_OWNER}"* ]]; }; then
  printf 'Existing provider condition already permits %s.\n' "${GITHUB_REPOSITORY}"
elif [[ "${PROVIDER_CONDITION}" == *"${PIPELINE_REPOSITORY}"* ]]; then
  printf 'Updating the provider condition to permit both IAM repositories.\n'
  UPDATED_CONDITION="assertion.repository == '${PIPELINE_REPOSITORY}' || assertion.repository == '${GITHUB_REPOSITORY}'"
  gcloud iam workload-identity-pools providers update-oidc "${WIF_PROVIDER_ID}" \
    --workload-identity-pool="${WIF_POOL_ID}" \
    --location=global \
    --project="${PROJECT_ID}" \
    --attribute-condition="${UPDATED_CONDITION}"
else
  printf 'Current provider condition: %s\n' "${PROVIDER_CONDITION:-<empty>}" >&2
  die "The existing WIF condition is unfamiliar. It was not modified."
fi

WIF_PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/attribute.repository/${GITHUB_REPOSITORY}"
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --member="${WIF_PRINCIPAL}" \
  --role="roles/iam.workloadIdentityUser" \
  --condition=None \
  --quiet \
  --format=none

info "CLOUD SQL INSTANCE"
SQL_DESCRIBE_ERROR="$(mktemp)"
if gcloud sql instances describe "${SQL_INSTANCE}" \
  --project="${PROJECT_ID}" >/dev/null 2>"${SQL_DESCRIBE_ERROR}"; then
  printf 'Using existing Cloud SQL instance: %s\n' "${SQL_INSTANCE}"
else
  if ! grep -Eqi 'does not exist|not found|HTTPError 404' "${SQL_DESCRIBE_ERROR}"; then
    sed -n '1,20p' "${SQL_DESCRIBE_ERROR}" >&2
    rm -f "${SQL_DESCRIBE_ERROR}"
    die "Cloud SQL could not be inspected. This is an API or permission problem, not a missing resource."
  fi

  rm -f "${SQL_DESCRIBE_ERROR}"
  printf '\nCloud SQL is billable continuously until you explicitly delete the instance.\n'
  printf 'This creates a small, zonal, 10 GB PostgreSQL demo instance with backups disabled.\n'
  read -r -p "Type CREATE to create ${SQL_INSTANCE}: " CREATE_CONFIRMATION
  [[ "${CREATE_CONFIRMATION}" == "CREATE" ]] || die "Cloud SQL creation cancelled."

  gcloud sql instances create "${SQL_INSTANCE}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --database-version=POSTGRES_16 \
    --edition=enterprise \
    --tier=db-f1-micro \
    --availability-type=zonal \
    --storage-type=SSD \
    --storage-size=10 \
    --no-storage-auto-increase \
    --no-backup \
    --no-deletion-protection \
    --assign-ip \
    --connector-enforcement=REQUIRED \
    --quiet
fi
rm -f "${SQL_DESCRIBE_ERROR}"

SQL_STATE="$(gcloud sql instances describe "${SQL_INSTANCE}" \
  --project="${PROJECT_ID}" --format='value(state)')"
[[ "${SQL_STATE}" == "RUNNABLE" ]] \
  || die "Cloud SQL exists but is not ready. Current state: ${SQL_STATE}"

info "POSTGRESQL DATABASE"
if gcloud sql databases describe "${SQL_DATABASE}" \
  --instance="${SQL_INSTANCE}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  printf 'Using existing database: %s\n' "${SQL_DATABASE}"
else
  gcloud sql databases create "${SQL_DATABASE}" \
    --instance="${SQL_INSTANCE}" \
    --project="${PROJECT_ID}" \
    --charset=UTF8
fi

SQL_USER_EXISTS=false
if gcloud sql users list \
  --instance="${SQL_INSTANCE}" \
  --project="${PROJECT_ID}" \
  --filter="name=${SQL_USER}" \
  --format='value(name)' | grep -Fxq "${SQL_USER}"; then
  SQL_USER_EXISTS=true
fi

SECRET_EXISTS=false
if gcloud secrets describe "${DATABASE_SECRET}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  SECRET_EXISTS=true
fi

info "DATABASE USER AND SECRET"
if [[ "${SQL_USER_EXISTS}" == "true" && "${SECRET_EXISTS}" == "true" ]]; then
  printf 'Database user and secret already exist; credentials were not rotated.\n'
else
  DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  ENCODED_DB_PASSWORD="$(DB_PASSWORD="${DB_PASSWORD}" python3 -c 'import os,urllib.parse; print(urllib.parse.quote(os.environ["DB_PASSWORD"], safe=""))')"

  if [[ "${SQL_USER_EXISTS}" == "true" ]]; then
    printf 'The user exists but the secret is missing; rotating the user password.\n'
    gcloud sql users set-password "${SQL_USER}" \
      --instance="${SQL_INSTANCE}" \
      --project="${PROJECT_ID}" \
      --password="${DB_PASSWORD}" \
      --quiet
  else
    gcloud sql users create "${SQL_USER}" \
      --instance="${SQL_INSTANCE}" \
      --project="${PROJECT_ID}" \
      --password="${DB_PASSWORD}" \
      --quiet
  fi

  DATABASE_URL="postgresql+psycopg://${SQL_USER}:${ENCODED_DB_PASSWORD}@/${SQL_DATABASE}?host=/cloudsql/${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
  if [[ "${SECRET_EXISTS}" == "true" ]]; then
    printf '%s' "${DATABASE_URL}" | gcloud secrets versions add "${DATABASE_SECRET}" \
      --project="${PROJECT_ID}" \
      --data-file=- \
      --quiet
  else
    printf '%s' "${DATABASE_URL}" | gcloud secrets create "${DATABASE_SECRET}" \
      --project="${PROJECT_ID}" \
      --replication-policy=automatic \
      --data-file=- \
      --quiet
  fi

  unset DB_PASSWORD ENCODED_DB_PASSWORD DATABASE_URL
fi

gcloud secrets add-iam-policy-binding "${DATABASE_SECRET}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None \
  --quiet \
  --format=none

info "FINAL VERIFICATION"
gcloud iam service-accounts describe "${DEPLOY_SA_EMAIL}" \
  --project="${PROJECT_ID}" --format='value(email)'
gcloud iam service-accounts describe "${RUNTIME_SA_EMAIL}" \
  --project="${PROJECT_ID}" --format='value(email)'
gcloud sql instances describe "${SQL_INSTANCE}" \
  --project="${PROJECT_ID}" --format='table(name,state,connectionName)'
gcloud sql databases describe "${SQL_DATABASE}" \
  --instance="${SQL_INSTANCE}" \
  --project="${PROJECT_ID}" --format='value(name)'
gcloud secrets versions list "${DATABASE_SECRET}" \
  --project="${PROJECT_ID}" \
  --filter='state=ENABLED' \
  --limit=1 \
  --format='table(name,state,createTime)'

info "GITHUB DEV ENVIRONMENT VALUES"
cat <<VALUES
GCP_PROJECT_ID=${PROJECT_ID}
GCP_REGION=${REGION}
WIF_PROVIDER=${WIF_PROVIDER}
DEPLOY_SERVICE_ACCOUNT=${DEPLOY_SA_EMAIL}
RUNTIME_SERVICE_ACCOUNT=${RUNTIME_SA_EMAIL}
CLOUD_RUN_SERVICE=iam-agent
CLOUD_SQL_INSTANCE=${PROJECT_ID}:${REGION}:${SQL_INSTANCE}
DATABASE_SECRET=${DATABASE_SECRET}
GOOGLE_CLOUD_LOCATION=${REGION}
GEMINI_MODEL=gemini-2.5-flash
BIGQUERY_PROJECT=${PROJECT_ID}
BIGQUERY_DATASET=iam_data_dev
BIGQUERY_ROWS_PER_TABLE=5
CHAT_DEFAULT_SCOPE=/company
VALUES

printf '\nGCP SETUP COMPLETED\n'
printf 'Next: add the printed values plus your Chat principal emails to the GitHub dev Environment.\n'
printf 'Then run Actions > Deploy IAM Agent.\n'
printf 'Reminder: this script never deletes Cloud SQL; delete the instance manually when the demo is no longer needed.\n'
