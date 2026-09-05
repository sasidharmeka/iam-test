# One-click deployment and Google Chat setup

This guide deploys the IAM agent from the GitHub **Actions** tab. The upstream
`IAM_NEW_1` repository remains responsible for refreshing the BigQuery analytics tables.

## What the deployment workflow does

`.github/workflows/deploy-agent.yml` provides a manual **Deploy IAM Agent** workflow. It:

1. validates the GitHub Environment configuration;
2. runs compilation, Ruff, and the offline tests;
3. authenticates to Google Cloud using Workload Identity Federation (WIF);
4. enables the required Google Cloud APIs;
5. confirms that the runtime service account, Cloud SQL instance, and database secret exist;
6. builds and deploys a private Cloud Run service with scale-to-zero and a maximum of one instance;
7. injects the BigQuery, Gemini, Chat authorization, and Cloud SQL configuration;
8. grants only Google Chat's service agent permission to invoke the service; and
9. prints the Google Chat HTTP endpoint in the workflow summary.

The workflow never creates or deletes a Cloud SQL instance and never prints the database URL.

## One-time Google Cloud prerequisites

The following resources must exist before GitHub can deploy. If they already exist, do not
create duplicates.

### 1. WIF provider and deployment service account

The existing provider can be reused:

```text
projects/427442249843/locations/global/workloadIdentityPools/github-actions/providers/iam-repo
```

Reuse means that both repositories use the same identity bridge. The provider or its IAM
binding must also trust this repository:

```text
sasidharmeka/multi-agent-demo
```

If the provider maps `attribute.repository`, an administrator can add the repository-specific
binding to the deployment service account:

```bash
gcloud iam service-accounts add-iam-policy-binding DEPLOY_SERVICE_ACCOUNT \
  --project=rapid-stage-506406-t4 \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/427442249843/locations/global/workloadIdentityPools/github-actions/attribute.repository/sasidharmeka/multi-agent-demo"
```

If the provider itself has a condition that permits only `sasidharmeka/IAM_NEW_1`, update that
condition to permit both repositories. Adding a service-account binding cannot override a
provider-level condition.

For source deployments and the automatic Google Chat invoker binding, the deployment service
account needs these project roles:

```text
roles/run.admin
roles/run.sourceDeveloper
roles/serviceusage.serviceUsageAdmin
roles/serviceusage.serviceUsageConsumer
roles/cloudsql.viewer
roles/secretmanager.viewer
```

It also needs `roles/iam.serviceAccountUser` on the runtime service account. Google Cloud's
default build service account (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`) needs
`roles/run.builder` for `gcloud run deploy --source`.

### 2. Cloud Run runtime service account

Use a separate least-privilege identity, for example:

```text
iam-agent-runtime@rapid-stage-506406-t4.iam.gserviceaccount.com
```

It needs:

```text
roles/bigquery.jobUser
roles/bigquery.dataViewer
roles/aiplatform.user
roles/cloudsql.client
```

Grant `roles/secretmanager.secretAccessor` only on the database secret. For a stricter
production setup, grant BigQuery Data Viewer only on `iam_data_dev` instead of the entire
project.

### 3. Cloud SQL PostgreSQL

Create a small PostgreSQL instance in `us-central1`. Suggested demo names are:

```text
Instance: iam-agent-db
Database: iam_agent
User:     iam_agent_app
```

The resulting connection name is:

```text
rapid-stage-506406-t4:us-central1:iam-agent-db
```

The application creates its `action_queue`, audit, and simulation tables automatically. It
does not create the Cloud SQL instance, database, or database user.

### 4. Secret Manager database URL

Create a Secret Manager secret called `IAM_AGENT_DATABASE_URL` with this value on one line:

```text
postgresql+psycopg://iam_agent_app:URL_ENCODED_PASSWORD@/iam_agent?host=/cloudsql/rapid-stage-506406-t4:us-central1:iam-agent-db
```

Percent-encode characters in the password that have a special meaning in a URL. Do not save
this URL in GitHub, `.env`, workflow logs, or source control. Grant the runtime service account
Secret Manager Secret Accessor on this secret.

## GitHub Environment variables

Open the `multi-agent-demo` repository and go to:

**Settings > Environments > New environment > `dev`**

Create these environment variables:

| Variable | Example value | Required |
| --- | --- | --- |
| `GCP_PROJECT_ID` | `rapid-stage-506406-t4` | Yes |
| `GCP_REGION` | `us-central1` | No; defaults to this value |
| `WIF_PROVIDER` | `projects/427442249843/locations/global/workloadIdentityPools/github-actions/providers/iam-repo` | Yes |
| `DEPLOY_SERVICE_ACCOUNT` | `YOUR_DEPLOYER@rapid-stage-506406-t4.iam.gserviceaccount.com` | Yes |
| `RUNTIME_SERVICE_ACCOUNT` | `iam-agent-runtime@rapid-stage-506406-t4.iam.gserviceaccount.com` | Yes |
| `CLOUD_RUN_SERVICE` | `iam-agent` | No; defaults to this value |
| `CLOUD_SQL_INSTANCE` | `rapid-stage-506406-t4:us-central1:iam-agent-db` | Yes |
| `DATABASE_SECRET` | `IAM_AGENT_DATABASE_URL` | No; defaults to this value |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | No; defaults to this value |
| `GEMINI_MODEL` | `gemini-2.5-flash` | No; defaults to this value |
| `BIGQUERY_PROJECT` | `rapid-stage-506406-t4` | Yes |
| `BIGQUERY_DATASET` | `iam_data_dev` | Yes |
| `BIGQUERY_ROWS_PER_TABLE` | `5` | No; valid range is 1-50 |
| `CHAT_ANALYST_PRINCIPALS` | `analyst@yourdomain.com` | Yes |
| `CHAT_APPROVER_PRINCIPALS` | `approver@yourdomain.com` | Yes |
| `CHAT_ADMIN_PRINCIPALS` | `admin@yourdomain.com` | No |
| `CHAT_DEFAULT_SCOPE` | `/company` | No; defaults to this value |

Multiple Chat principals are comma-separated. Use different accounts for the requester and
approver because the approval gateway rejects self-approval.

WIF does not require a JSON service-account key, so do not create a `GCP_SA_KEY` secret. The
database URL stays in Google Secret Manager, not GitHub Secrets.

## Run from GitHub

The workflow file must be committed to the repository's default branch (normally `main`)
before GitHub displays its **Run workflow** button.

1. Run the `IAM_NEW_1` **full-pipeline** workflow and confirm that it populated
   `rapid-stage-506406-t4.iam_data_dev`.
2. Open **multi-agent-demo > Actions > Deploy IAM Agent**.
3. Click **Run workflow**.
4. Select `dev` and confirm.
5. When it completes, open its job summary and copy the displayed Google Chat HTTP endpoint.

The deployment action is needed only for the first deployment and later code/configuration
changes. A Chat message wakes the scale-to-zero Cloud Run service; it does not require another
GitHub workflow run.

## Register the Google Chat application

Google Chat registration is a one-time console operation:

1. In project `rapid-stage-506406-t4`, open **APIs & Services > Google Chat API > Configuration**.
2. Set the app name, avatar URL, and description.
3. Enable **Interactive features**.
4. Select **HTTP endpoint URL**.
5. Paste the workflow's endpoint, which ends in `/google-chat/events`.
6. Choose the HTTP endpoint URL as the authentication audience.
7. Add the analyst and approver Workspace accounts under **Visibility**.
8. Set the app status to **Live - available to users** and save.
9. Open <https://chat.google.com>, click **New chat**, and search for the configured app name.

Send:

```text
help
```

Then send an identity that exists in `config/identities.yaml`, for example:

```text
investigate user1
```

Google Chat requires a supported Google Workspace account. If the organization blocks custom
Chat applications, a Workspace administrator must allow it.

## WIF versus Cloud SQL

WIF and Cloud SQL are not alternatives:

- **WIF answers “who is GitHub?”** It exchanges GitHub's short-lived identity for permission to
  call Google Cloud APIs. No long-lived GCP key is stored in GitHub.
- **Cloud SQL answers “where is durable action state stored?”** It stores pending approvals,
  decisions, execution results, verification state, and audit records.
- **The Cloud Run runtime service account answers “who is the running agent?”** It is authorized
  to reach BigQuery, Gemini, Secret Manager, and the Cloud SQL connector.
- **The PostgreSQL credential answers “which database user can open this database session?”**
  The current implementation reads it from the Secret Manager URL.

Cloud SQL can use IAM database authentication, but that would use the Cloud Run runtime service
account—not GitHub WIF—and would require a different connection/token-refresh implementation.
It would still require a Cloud SQL database because WIF cannot store rows.

## Common failures

| Failure | Meaning |
| --- | --- |
| `iam.serviceAccounts.getAccessToken denied` | WIF or the deployment-service-account binding does not trust `sasidharmeka/multi-agent-demo`. |
| `iam.serviceAccounts.actAs denied` | The deployer lacks Service Account User on the runtime service account. |
| `run.services.setIamPolicy denied` | The deployer cannot grant Google Chat the Cloud Run Invoker role. |
| Cloud Build permission error | The default build service account lacks `roles/run.builder`. |
| Secret not found/access denied | `DATABASE_SECRET` is wrong or the runtime account lacks Secret Accessor. |
| Cloud SQL connection error | The connection name, database URL, database user, or password is incorrect. |
| BigQuery table not found | Run the full `IAM_NEW_1` pipeline or correct `BIGQUERY_PROJECT`/`BIGQUERY_DATASET`. |
| `You are not authorized` | The Chat sender's exact Workspace email is absent from the Chat principal variables. |
| App cannot be found in Chat | Add the user under Chat API Visibility and set the app status to Live. |
