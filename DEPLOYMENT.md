# Deploy the read-only IAM agent

This guide covers the complete path from a fresh Google Cloud Shell to the authenticated web
interface. The GitHub Actions workflow builds and deploys the application after the one-time
Google Cloud setup.

## Updating an already working deployment to 3.2

No new environment variables, secrets, IAM roles, or database are required for catalog-first
routing, evidence coverage checks, or report exports. Keep the existing `dev` values. Run
**Actions > Deploy read-only IAM agent > Run workflow**, select branch `main` and environment
`dev`, and wait for success. Reload the existing Cloud Run web page.

Open the service's `/healthz` URL. It should show `"version":"3.2"`, the deployed revision, and
four MCP tools including `search_iam_catalog`. This confirms which code is serving requests;
health alone does not verify live Gemini or BigQuery access. Test these exact requests:

```text
Which users are riskiest, and why?
Tell me about user12.
Show MFA bypasses and stale access for user12.
```

If a data question still fails, the response now names the stage and error category. From an
existing authenticated Cloud Shell checkout, inspect its reference without changing resources:

```bash
cd "${HOME}/multi-agent-iam-demo"
git pull --ff-only origin main
bash scripts/diagnose_iam_agent.sh
# To restrict the last seven days of logs to one response, supply its reference:
bash scripts/diagnose_iam_agent.sh cae4f48ad410
bash scripts/diagnose_iam_agent.sh d3057a9fd55d
```

Use a reference from a new response if the older logs have expired. The helper displays the
active account, service revision, runtime identity, and application errors. It does not change
permissions or query identity rows. `bigquery_permission`, `bigquery_not_found`, `bigquery_schema`,
`gemini_request_rejected`, and `gemini_permission` identify different failure categories; the
logged underlying exception supplies the specific cause. A greeting succeeding only verifies
the general-answer path, not the structured planning and BigQuery path.

## What changed from the remediation demo

The read-only application does not use:

- Cloud SQL or `CLOUD_SQL_INSTANCE`;
- Secret Manager or `DATABASE_SECRET`/`DATABASE_URL`;
- analyst/approver separation or any `*_APPROVER_PRINCIPALS` setting;
- an action queue, approval cards, or decision endpoint; or
- Saviynt/AD mutation permissions.

The only continuing billable data services are Cloud Run when handling requests, Gemini model
calls, and BigQuery queries/storage. Cloud Run is configured with zero minimum instances and one
maximum instance. Every BigQuery step is dry-run first and limited by
`QUERY_MAX_BYTES_BILLED`.

## Prerequisites

- You are signed into Cloud Shell with an account that can administer project
  `rapid-stage-506406-t4` (Owner is sufficient for this demo setup).
- The default GitHub branch contains this version of the repository.
- The `IAM_NEW_1` pipeline has created dataset `iam_data_dev` and its 22 outputs.
- You have an OAuth web client ID ending in `.apps.googleusercontent.com`. The client ID is not
  a secret. Do not use or upload the OAuth client secret.

## Step 1: run the one-time Cloud Shell setup

Open [Google Cloud Shell](https://shell.cloud.google.com/), then paste this entire block:

```bash
set -euo pipefail

gcloud config set project rapid-stage-506406-t4
gcloud config list --format='text(core.account,core.project)'
gcloud projects describe rapid-stage-506406-t4 \
  --format='table(projectId,projectNumber,lifecycleState)'

cd "${HOME}"
if [[ -d multi-agent-iam-demo/.git ]]; then
  git -C multi-agent-iam-demo pull --ff-only
else
  git clone https://github.com/sasidharmeka/multi-agent-iam-demo.git
fi

cd "${HOME}/multi-agent-iam-demo"
chmod +x setup_iam_agent.sh
./setup_iam_agent.sh 2>&1 | tee "${HOME}/setup_iam_agent.log"
```

The script is idempotent and performs these actions:

1. verifies the active account and target project;
2. enables Vertex AI, BigQuery, Cloud Run, Cloud Build, IAM, STS, and related APIs;
3. creates or reuses `iam-agent-deploy` and `iam-agent-runtime` service accounts;
4. grants the runtime account Gemini and read-only BigQuery access;
5. grants the deployer Cloud Run source-deployment permissions and Service Account User on the
   runtime account;
6. discovers the project's actual default Cloud Build account, grants it `roles/run.builder`,
   and gives the deployer `actAs` permission on it;
7. creates or safely updates the GitHub WIF pool/provider to trust both `IAM_NEW_1` and
   `multi-agent-iam-demo`;
8. binds this repository's WIF principal to the deployer; and
9. checks whether `iam_data_dev` exists and prints every GitHub Environment variable.

It does not start a background job. If Cloud Shell closes while it is running, that shell process
stops. Open a new shell and rerun the same block; completed steps are reused.

### How to recognize success

The last heading is:

```text
===== SETUP COMPLETE =====
```

and the command returns to a prompt without an `ERROR: setup failed` message. The full output is
also in `~/setup_iam_agent.log` for that Cloud Shell home directory.

## Step 2: create the GitHub `dev` Environment

Open the GitHub repository and go to:

**Settings > Environments > New environment > `dev`**

Create these Environment variables, not repository secrets:

| Variable | Value | Required |
| --- | --- | --- |
| `GCP_PROJECT_ID` | `rapid-stage-506406-t4` | Yes |
| `GCP_REGION` | `us-central1` | No; default shown |
| `WIF_PROVIDER` | Printed by the setup script; expected `projects/427442249843/locations/global/workloadIdentityPools/github-actions/providers/iam-repo` for this project | Yes |
| `DEPLOY_SERVICE_ACCOUNT` | `iam-agent-deploy@rapid-stage-506406-t4.iam.gserviceaccount.com` | Yes |
| `RUNTIME_SERVICE_ACCOUNT` | `iam-agent-runtime@rapid-stage-506406-t4.iam.gserviceaccount.com` | Yes |
| `CLOUD_RUN_SERVICE` | `iam-agent` | No; default shown |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | No; default shown |
| `GEMINI_MODEL` | `gemini-2.5-flash` | No; default shown |
| `BIGQUERY_PROJECT` | `rapid-stage-506406-t4` | Yes |
| `BIGQUERY_DATASET` | `iam_data_dev` | Yes |
| `QUERY_MAX_ROWS` | `500` | No; valid range 1-5000 |
| `QUERY_MAX_BYTES_BILLED` | `500000000` | No; 500 MB per query step |
| `QUERY_MAX_LOOKBACK_DAYS` | `90` | No |
| `QUERY_MAX_STEPS` | `6` | No; maximum 8 |
| `WEB_GOOGLE_CLIENT_ID` | Your existing OAuth web client ID | Yes |
| `WEB_ANALYST_PRINCIPALS` | Your exact Gmail or Workspace email; comma-separate multiple users | Yes |
| `CHAT_ANALYST_PRINCIPALS` | Workspace email(s), or leave blank if not using Google Chat | No |
| `QUERY_DEFAULT_SCOPE` | `/company` | No; default shown |

That is the complete variable list. In particular, do not create `DATABASE_SECRET`,
`CLOUD_SQL_INSTANCE`, `DATABASE_URL`, `CHAT_APPROVER_PRINCIPALS`, or
`WEB_APPROVER_PRINCIPALS`.

CSV, Excel, PDF, and Word downloads require no additional GitHub or Cloud Run variables. They
are generated from the already-returned evidence and held briefly in application memory.

The workflow itself sets `ENABLE_WEB_UI=true`, discovers the final Chat audience URL, and enables
Google Chat request-token verification.

### Optional: set variables with GitHub CLI

If `gh` is installed and authenticated in Cloud Shell, replace the two placeholders and paste:

```bash
export WEB_CLIENT_ID='YOUR_CLIENT_ID.apps.googleusercontent.com'
export ANALYST_EMAIL='YOUR_GOOGLE_ACCOUNT_EMAIL'
export REPOSITORY='sasidharmeka/multi-agent-iam-demo'

gh variable set GCP_PROJECT_ID --env dev --repo "${REPOSITORY}" --body 'rapid-stage-506406-t4'
gh variable set GCP_REGION --env dev --repo "${REPOSITORY}" --body 'us-central1'
gh variable set WIF_PROVIDER --env dev --repo "${REPOSITORY}" \
  --body 'projects/427442249843/locations/global/workloadIdentityPools/github-actions/providers/iam-repo'
gh variable set DEPLOY_SERVICE_ACCOUNT --env dev --repo "${REPOSITORY}" \
  --body 'iam-agent-deploy@rapid-stage-506406-t4.iam.gserviceaccount.com'
gh variable set RUNTIME_SERVICE_ACCOUNT --env dev --repo "${REPOSITORY}" \
  --body 'iam-agent-runtime@rapid-stage-506406-t4.iam.gserviceaccount.com'
gh variable set CLOUD_RUN_SERVICE --env dev --repo "${REPOSITORY}" --body 'iam-agent'
gh variable set GOOGLE_CLOUD_LOCATION --env dev --repo "${REPOSITORY}" --body 'us-central1'
gh variable set GEMINI_MODEL --env dev --repo "${REPOSITORY}" --body 'gemini-2.5-flash'
gh variable set BIGQUERY_PROJECT --env dev --repo "${REPOSITORY}" --body 'rapid-stage-506406-t4'
gh variable set BIGQUERY_DATASET --env dev --repo "${REPOSITORY}" --body 'iam_data_dev'
gh variable set QUERY_MAX_ROWS --env dev --repo "${REPOSITORY}" --body '500'
gh variable set QUERY_MAX_BYTES_BILLED --env dev --repo "${REPOSITORY}" --body '500000000'
gh variable set QUERY_MAX_LOOKBACK_DAYS --env dev --repo "${REPOSITORY}" --body '90'
gh variable set QUERY_MAX_STEPS --env dev --repo "${REPOSITORY}" --body '6'
gh variable set WEB_GOOGLE_CLIENT_ID --env dev --repo "${REPOSITORY}" --body "${WEB_CLIENT_ID}"
gh variable set WEB_ANALYST_PRINCIPALS --env dev --repo "${REPOSITORY}" --body "${ANALYST_EMAIL}"
gh variable set QUERY_DEFAULT_SCOPE --env dev --repo "${REPOSITORY}" --body '/company'
```

Do not paste an OAuth client secret into any command or GitHub setting.

## Step 3: verify the OAuth web client

In project `rapid-stage-506406-t4`:

1. Open **Google Auth Platform > Branding** and complete the app name, support email, and
   developer contact.
2. Under **Audience**, choose **External** for personal Gmail testing, keep the app in Testing,
   and add every test Gmail address.
3. Under **Clients**, open the Web application client whose ID is in
   `WEB_GOOGLE_CLIENT_ID`.
4. Add the exact Cloud Run base URL to **Authorized JavaScript origins** after the first deploy.
   Use only `https://HOST`, with no trailing slash or path.

If the service has already been deployed, get the base URL in Cloud Shell:

```bash
gcloud run services describe iam-agent \
  --project=rapid-stage-506406-t4 \
  --region=us-central1 \
  --format='value(status.url)'
```

For a first deployment when no URL exists, temporarily use `http://localhost:8080` as an origin,
deploy, add the resulting Cloud Run base URL, and then reload the page. Redeployment is needed
only if the client ID itself changes; changing the allowed origin in the same OAuth client does
not change the Cloud Run environment variable.

## Step 4: deploy from GitHub Actions

1. Open **Actions** in `sasidharmeka/multi-agent-iam-demo`.
2. Select **Deploy read-only IAM agent**.
3. Click **Run workflow**, select `dev`, and run it.
4. Open the successful job summary. It shows the web URL, health URL, and optional Chat endpoint.

The workflow runs compilation, Ruff, and tests before authenticating with WIF. It then deploys a
single scale-to-zero Cloud Run service from source. No JSON service-account key is used.

Run it again only after code or deployment-variable changes. Normal browser requests wake Cloud
Run automatically.

## Step 5: test the web interface

Open the base Cloud Run URL shown in the workflow summary. Sign in using an exact email listed in
`WEB_ANALYST_PRINCIPALS`, then try:

```text
What is shadow access?
Who are the ten riskiest users?
Why is user12 risky?
Show user12's timeline.
Compare MFA bypasses by application.
Which Finance users have stale access?
Show the riskiest users by application and export as CSV.
Analyze user16 and give me Excel and PDF reports.
Compare MFA bypasses by application in a Word document.
```

The first question can be answered generally. The other questions cause MCP catalog discovery,
a typed Gemini plan, deterministic SQL compilation, a BigQuery dry run, a bounded query, and a
grounded response. An explicit CSV, Excel/XLSX, PDF, or Word/DOCX request adds download buttons
below the answer. Download the files within 15 minutes; a Cloud Run instance restart also clears
them. The endpoint re-verifies the signed-in Google account and never returns another user's
report.

## Step 6: optionally configure Google Chat

The web interface is sufficient for personal Gmail testing. Google Chat app discovery usually
requires a supported Workspace account and may be restricted by a Workspace administrator.

In **Google Chat API > Configuration**:

1. provide an app name, description, and any publicly reachable HTTPS avatar image;
2. enable interactive features and direct messages;
3. under Triggers, choose a common HTTP endpoint for all triggers;
4. enter `https://YOUR_CLOUD_RUN_HOST/google-chat/events`;
5. choose the HTTP endpoint URL as the authentication audience;
6. restrict Visibility to the Workspace accounts listed in `CHAT_ANALYST_PRINCIPALS`;
7. set the app status to live/available and save; and
8. find the app under **New chat** at [chat.google.com](https://chat.google.com).

All message, added-to-space, and removed-from-space events use the same endpoint. There is no
approval trigger because the application is read-only.

## Verify everything from a new Cloud Shell

If the original shell closed, open a new one and paste:

```bash
set -u
PROJECT_ID='rapid-stage-506406-t4'
REGION='us-central1'

gcloud config set project "${PROJECT_ID}"
gcloud config list --format='text(core.account,core.project)'

echo '===== ENABLED APIS ====='
gcloud services list --enabled --project="${PROJECT_ID}" \
  --filter='name:(aiplatform.googleapis.com OR bigquery.googleapis.com OR cloudbuild.googleapis.com OR run.googleapis.com)' \
  --format='table(config.name)'

echo '===== SERVICE ACCOUNTS ====='
gcloud iam service-accounts describe \
  "iam-agent-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --project="${PROJECT_ID}" --format='value(email)'
gcloud iam service-accounts describe \
  "iam-agent-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --project="${PROJECT_ID}" --format='value(email)'

echo '===== BUILD ACCOUNT ====='
gcloud builds get-default-service-account --project="${PROJECT_ID}"

echo '===== WIF PROVIDER ====='
gcloud iam workload-identity-pools providers describe iam-repo \
  --workload-identity-pool=github-actions \
  --location=global \
  --project="${PROJECT_ID}" \
  --format='yaml(name,state,attributeMapping,attributeCondition)'

echo '===== BIGQUERY OUTPUTS ====='
bq --project_id="${PROJECT_ID}" ls "${PROJECT_ID}:iam_data_dev"

echo '===== CLOUD RUN ====='
gcloud run services describe iam-agent \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='table(metadata.name,status.url,status.latestReadyRevisionName)'

SERVICE_URL="$(gcloud run services describe iam-agent \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"
if [[ -n "${SERVICE_URL}" ]]; then
  curl --fail --silent --show-error "${SERVICE_URL}/healthz"
fi
```

A healthy response includes `"mode":"read-only"`, `"version":"3.2"`, the revision, and the four
MCP tool names.

View recent application logs with:

```bash
gcloud run services logs read iam-agent \
  --project=rapid-stage-506406-t4 \
  --region=us-central1 \
  --limit=100
```

## Common failures

| Failure | Cause and fix |
| --- | --- |
| `gcloud config list account project` says unrecognized arguments | Use `gcloud config list --format='text(core.account,core.project)'` as shown above. |
| API prompt asks `Would you like to enable?` | Enter `y`, or rerun `setup_iam_agent.sh`; it enables all required APIs non-interactively. |
| Resource is `NOT_FOUND` during an early diagnostic | The old setup had not created it. Run the new idempotent setup script; Cloud SQL is no longer expected. |
| `client is not authorized` despite Owner appearing in Console | Confirm the active CLI email and exact project with `gcloud config list`; refresh Cloud Shell authorization or run `gcloud auth login` if it is the wrong identity. Organization deny policies can still override Owner. |
| WIF `credential is rejected by the attribute condition` | The provider does not trust `sasidharmeka/multi-agent-iam-demo`. Rerun the setup script and inspect the WIF provider verification output. |
| Build says caller cannot act as a service account | The deployer lacks Service Account User on the project's selected Cloud Build account. The new setup script discovers and grants this exact binding. |
| BigQuery dataset/table not found | Run the `IAM_NEW_1` full pipeline and confirm its target is `rapid-stage-506406-t4.iam_data_dev`. |
| BigQuery permission denied | Confirm the runtime account has BigQuery Job User and Data Viewer and the Cloud Run revision uses that runtime account. |
| Query rejected due to bytes | Narrow the time range/question or deliberately increase `QUERY_MAX_BYTES_BILLED` and redeploy. |
| Sign-in says origin is not allowed | Add the exact Cloud Run base URL under the OAuth Web client's Authorized JavaScript origins. |
| Sign-in succeeds but chat says unauthorized | Add the exact verified email to `WEB_ANALYST_PRINCIPALS` or `CHAT_ANALYST_PRINCIPALS`, then redeploy. |
| Google Chat app cannot be found | Confirm Workspace support, Chat API Visibility, live status, and administrator policy; personal Gmail can use the separate browser interface. |
| Health is good but an answer fails | Read Cloud Run logs; common causes are missing pipeline outputs, a query byte cap, or Vertex AI quota/permissions. |

## Optional cleanup of the old Cloud SQL demo

The refactored service does not automatically delete the old `iam-agent-db` instance or
`IAM_AGENT_DATABASE_URL` secret. Cloud SQL can continue billing while it exists, even when unused.

First deploy this read-only version successfully and confirm its Cloud Run configuration has no
`DATABASE_URL` or Cloud SQL attachment:

```bash
gcloud run services describe iam-agent \
  --project=rapid-stage-506406-t4 \
  --region=us-central1 \
  --format=export | grep -E 'DATABASE_URL|cloudsql|IAM_AGENT_DATABASE_URL' || true
```

If that prints nothing and you do not need the old demo state, these commands are destructive and
Google Cloud will ask for confirmation:

```bash
gcloud sql instances delete iam-agent-db \
  --project=rapid-stage-506406-t4

gcloud secrets delete IAM_AGENT_DATABASE_URL \
  --project=rapid-stage-506406-t4
```

Deleting the instance permanently removes its databases unless you first export or back them up.
These cleanup commands are never executed by the application, setup script, or deployment
workflow.
