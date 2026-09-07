# Read-only conversational IAM analytics agent

This repository turns the 22 BigQuery outputs produced by
[`sasidharmeka/IAM_NEW_1`](https://github.com/sasidharmeka/IAM_NEW_1) into a
conversational IAM analyst. A user can ask broad questions such as:

- "Which users have the highest risk scores?"
- "Why is user12 risky?"
- "Compare MFA bypasses by application."
- "What changed for user12 in the last 30 days?"
- "Which Finance identities show role drift?"
- "What is shadow access?"
- "Export the ten riskiest users as Excel and PDF."

Before the first Gemini call, the MCP service searches all 22 catalog entries using the question
and relevant conversation history. It retrieves candidate schemas across multiple domains and
derives evidence requirements independently of Gemini. Gemini creates a typed semantic plan.
The MCP evidence service checks both question coverage and SQL safety, applies the user's
hierarchy scope, compiles parameterized `SELECT` statements, checks their estimated BigQuery
cost, and returns evidence. Gemini then explains that evidence in ordinary conversation.
For browser requests that explicitly name CSV, Excel, PDF, or Word, deterministic exporters
turn the same bounded answer and evidence into authenticated download files.

This version is intentionally read-only. It contains no remediation agent, approval gateway,
action queue, deterministic IAM mutation tool, Cloud SQL dependency, or database secret.

## Interfaces

| Interface | Authentication | Intended users |
| --- | --- | --- |
| Browser UI at the Cloud Run base URL | Google Identity Services ID token | Personal Gmail or Workspace accounts listed in `WEB_ANALYST_PRINCIPALS` |
| Optional Google Chat app | Google Chat OIDC token plus sender allowlist | Workspace users listed in `CHAT_ANALYST_PRINCIPALS` |

Both interfaces call the same FastAPI handler, orchestrator, MCP tools, and BigQuery controls.

## System at a glance

```mermaid
flowchart TD
    P["IAM_NEW_1 pipeline"] --> B["22 BigQuery outputs"]
    U["Browser UI or Google Chat"] --> F["FastAPI + authorization"]
    F --> O["IAM orchestrator"]
    O <--> G["Gemini planner and answerer"]
    O <--> M["In-process MCP evidence server"]
    M --> C["Catalog + validator + SQL compiler"]
    C --> B
    O --> R["CSV, Excel, PDF, Word exporters"]
```

The MCP client and server use the official MCP Python SDK. They run in the same Cloud Run
container, so MCP is a real protocol boundary without the cost or operational complexity of a
second service. The tool contract can later be moved to a separate service without changing
Gemini's semantic plan format.

## What is deterministic and what uses Gemini

| Component | Responsibility |
| --- | --- |
| Catalog retriever | Searches all concepts before Gemini; combines topic aliases, descriptions, fields, and typo-tolerant matches; retains multiple candidates |
| Gemini question planner | Resolves follow-ups and classifies general/data/mixed questions using already-retrieved schemas; domain labels do not gate data access |
| MCP catalog tools | Expose approved domain, concept, field, dimension, metric, and grain descriptions |
| Gemini query planner | Produces `AnalysisPlan` JSON using catalog vocabulary only |
| Pydantic models | Reject malformed or structurally unsafe plans |
| Query validator | Enforces catalog membership, organizational scope, row/time/step ceilings, and valid filters/sorts |
| Evidence coverage validator | Independently checks matched topics, explicit identity filters, and required explanatory fields before any SQL executes |
| SQL compiler | Maps trusted concepts and metrics to fixed tables and SQL expressions; the only code that creates SQL |
| BigQuery executor | Dry-runs each query, enforces bytes billed, executes read-only SQL, and returns rows |
| Gemini answerer | Explains only the returned evidence and cites only queried outputs |
| Report exporters | Deterministically render returned evidence; they cannot query BigQuery or execute formulas |

Gemini can choose among allowed analytical building blocks, but cannot invent a table, metric
expression, SQL fragment, or write operation.

## Repository layout

```text
iam_agent/
  api.py                 FastAPI wiring and HTTP routes
  authentication.py      Google web and Google Chat token verification
  authorization.py       Principal allowlist and hierarchy scopes
  planner.py             Gemini decomposition and semantic planning
  gemini_contracts.py    Simple provider schemas converted into strict application models
  answerer.py            General and evidence-grounded responses
  orchestrator.py        Conversation workflow
  mcp_client.py           Official in-process MCP client
  conversation.py        Bounded in-memory follow-up context
  reporting.py           Authenticated CSV/XLSX/PDF/DOCX generation and short-lived storage

iam_mcp/
  server.py              Four read-only MCP tools
  catalog.py             Semantic catalog loader
  retrieval.py           Whole-catalog candidate retrieval and evidence coverage checks
  validator.py           Plan, authorization, and resource validation
  compiler.py            Deterministic semantic-plan-to-SQL compiler
  bigquery_executor.py    Dry run, byte cap, and query execution
  service.py             MCP application service

config/
  semantic_catalog.yaml  All 22 pipeline outputs and their schemas/descriptions
  identities.yaml        Demo identity-to-hierarchy mapping
  authorization.yaml     Local fallback principal mapping
```

## MCP tools

| Tool | Input | Result | BigQuery access |
| --- | --- | --- | --- |
| `list_iam_domains` | None | Five routing cards covering all 22 concept names, meanings, entities, grains, and synonyms | None |
| `describe_iam_domains` | Selected domains | Relevant concepts, schemas, metrics, and rules | None |
| `search_iam_catalog` | Verified principal, original question, and bounded history | Candidate schemas, domains, matched topics, and evidence requirements | None |
| `run_iam_query` | Verified principal, original question/history, and typed `AnalysisPlan` | `EvidenceBundle` | Bounded `SELECT` only |

Gemini does not directly call BigQuery. The orchestrator makes MCP calls on Gemini's behalf and
passes the returned metadata/evidence back into Gemini's next structured-output call.

The planner receives candidate schemas before domain selection. An incorrect or empty domain
label cannot hide those schemas. Strong question matches become deterministic evidence
requirements: a login question needs authentication evidence; a broad user profile needs risk
scores and timelines; risk explanations need `risk_factors`. MCP recomputes these requirements
from the original question before executing a plan. A safe query against an unrelated table is
rejected. One repair attempt receives the entire catalog and the rejection reason, then passes
through all checks again. Ambiguous retrieval also keeps the entire small catalog available.

This first retrieval reads trusted catalog metadata, not private BigQuery rows. Actual rows are
retrieved only after the plan passes scope, coverage, SQL, and cost checks. Catalog matching is
not a proof of understanding every possible question; unfamiliar terminology can still need
clarification or a catalog alias. Gemini retains responsibility for analytical reasoning.

## Browser report exports

Ask for one or several formats in the same message:

- "Show the riskiest users by application and export as CSV."
- "Analyze user16 and give me Excel and PDF reports."
- "Compare MFA bypasses by application in a Word document."
- "Export this analysis in all report formats."

The browser displays one authenticated download button per requested format. Files contain the
answer, findings, sources, caveats, and the returned evidence rows. Spreadsheet-like text is
escaped to prevent formula injection. Downloads are bound to the Google account that created
them, kept only in the running instance's memory, and expire after 15 minutes. They are intended
for immediate download and require no Cloud SQL, GCS bucket, secret, or new environment variable.

## Five catalog domains

The 22 outputs are tagged into these discoverable topics. A concept can appear in more than one
domain when it supports cross-domain questions.

| Domain | Examples of covered evidence |
| --- | --- |
| `identity_risk` | Risk scores, cleanest users, suspicious sequences, high-impact findings |
| `authentication` | Impossible login/travel, MFA bypass, MFA drift, time-warp events |
| `access_and_provisioning` | Role drift, stale access, privilege escalation, rejected-but-executed, shadow access |
| `application_behavior` | Multi-app sessions, data transfer, application sequences, application usage |
| `lifecycle_and_data_quality` | Orphan users/events, identity timelines, governance completeness |

The catalog records each output's physical schema, business meaning, row grain, time field,
user field, safe dimensions, safe filters, metrics, synonyms, and default display fields.

## Quick deployment

The complete first-time commands, GitHub variables, OAuth setup, verification commands, and
troubleshooting guide are in [DEPLOYMENT.md](DEPLOYMENT.md). The shortest path is:

```bash
git clone https://github.com/sasidharmeka/multi-agent-iam-demo.git
cd multi-agent-iam-demo
chmod +x setup_iam_agent.sh
./setup_iam_agent.sh 2>&1 | tee ~/setup_iam_agent.log
```

Then set the GitHub Environment variables printed by the script and run
**Actions > Deploy read-only IAM agent > Run workflow**.

## Local tests

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
ruff check iam_agent iam_mcp tests main.py
pytest -q
```

Local end-to-end execution also needs Application Default Credentials with access to Gemini and
the BigQuery dataset. Copy `.env.example` to `.env`, fill in the web identity settings if needed,
then run:

```bash
uvicorn main:app --reload --port 8080
```

## Important data limitation

The agent can answer arbitrary questions that can be supported by the 22 outputs. It can also
answer general IAM questions without claiming they describe this project. It cannot truthfully
answer project-specific questions when the needed fact is absent from those outputs; in that
case it reports the gap. Some pipeline jobs overwrite their output tables, so a request for a
long historical trend is possible only when the relevant table actually retains that history.
Exporting a response changes its format, not the amount or scope of evidence available.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical sequence and safety invariants.
