# IAM Agent Platform Architecture

## Responsibility split

| Layer | Responsibility | May write? |
|---|---|---:|
| `IAM_NEW_1` | Ingest events, run Spark detections, build BigQuery analytics | BigQuery only |
| Google Chat + FastAPI | Accept user requests and approval clicks | No IAM systems |
| Authorization | Map actor role/scope and target hierarchy | No |
| IAM orchestrator | Collect evidence and coordinate Gemini agents | Action proposal only |
| Cloud SQL | Operational action state, decisions, audit, simulated state | Yes |
| Approval gateway | Enforce human authorization and state transitions | Cloud SQL only |
| Deterministic executor | Run exactly the approved operation | Simulated IAM only |

BigQuery is the analytical plane. Cloud SQL is the operational control plane. Keeping them
separate prevents chat retries or agent reasoning from mutating analytics.

## Investigation-to-recommendation sequence

```mermaid
sequenceDiagram
    participant U as Chat user
    participant C as Chat/FastAPI
    participant O as IAM orchestrator
    participant B as BigQuery
    participant A as Gemini agents
    participant Q as Cloud SQL

    U->>C: Investigate user12
    C->>C: Authenticate and authorize
    C->>O: Actor, target, Chat event ID
    O->>Q: Check event idempotency
    O->>B: Parameterized identity evidence query
    B-->>O: Risk, timeline, findings
    par Independent evidence analysis
        O->>A: Investigation
        O->>A: Policy/RAG
        O->>A: Impact analysis
    end
    O->>A: Remediation synthesis
    A-->>O: Typed operation or no_action
    O->>O: Validate target and evidence resource
    O->>Q: Insert PENDING_APPROVAL
    O-->>C: Review card
    C-->>U: Approve / Reject buttons
```

The three analysis calls are independent once the evidence bundle exists, so they run via
`asyncio.gather`. Remediation is dependent on all three and runs afterward. This is
concurrency for latency; it is not a set of long-running agent threads or microservices.

## Approval-to-verification sequence

```mermaid
sequenceDiagram
    participant H as Human approver
    participant C as Google Chat
    participant G as Approval gateway
    participant Q as Cloud SQL
    participant T as Deterministic tool

    H->>C: Click Approve
    C->>G: Actor + action_id
    G->>Q: Load immutable plan
    G->>G: Role, hierarchy, self-approval checks
    G->>Q: PENDING_APPROVAL -> APPROVED -> EXECUTING
    G->>T: Execute stored operation
    T->>Q: Update simulated IAM state
    T->>Q: Read state back and verify
    G->>Q: EXECUTED or FAILED + audit
    G-->>C: Updated status card
    C-->>H: Final result
```

## Action state machine

```mermaid
stateDiagram-v2
    [*] --> PENDING_APPROVAL
    PENDING_APPROVAL --> REJECTED: authorized rejection
    PENDING_APPROVAL --> APPROVED: authorized approval
    APPROVED --> EXECUTING
    EXECUTING --> EXECUTED: verification passes
    EXECUTING --> FAILED: execution or verification fails
```

Conditional SQL updates enforce these transitions. Replayed button clicks return the
terminal record and cannot run the tool twice.

## Agent contracts

| Agent | Inputs | Output | Explicitly cannot do |
|---|---|---|---|
| Investigation | Risk profile, anomaly rows, timeline | Findings with table + finding ID | Execute changes |
| Policy/RAG | Retrieved policy chunks and risk summary | Controls with exact chunk IDs | Invent policies |
| Impact analysis | Evidence bundle | Blast radius, urgency, action/inaction risks | Select a tool |
| Remediation | Three assessments and target identity | One typed plan or `no_action` | Approve or execute |

Gemini structured output is validated with Pydantic. Prompts delimit evidence and instruct
the model to treat retrieved values as data rather than instructions.

## Authorization hierarchy

`config/authorization.yaml` assigns every Chat principal a role and a scope. An actor may
act only when their scope is an ancestor of the target identity's path from
`config/identities.yaml`.

Example:

| Actor scope | Target scope | Result |
|---|---|---|
| `/company/finance` | `/company/finance/manager` | In scope |
| `/company/finance` | `/company/engineering/manager` | Denied |
| `/` | Any normalized target path | In scope |

Paths are compared by segments, so `/company/fin` is not an ancestor of
`/company/finance`.

## BigQuery contract with IAM_NEW_1

The evidence adapter reads the user-scoped outputs defined by
`IAM_NEW_1/spark_job/detections.py`. Every query uses `@target_user` and `@row_limit`. Table
identifiers come only from a source-code tuple; neither the user nor Gemini can select an
arbitrary table.

Global aggregations such as `ad_volume_spikes` and `ad_group_churn_rate` are intentionally
not pulled into a single-user investigation because they do not have a reliable `user`
filter.

## Trust boundaries

- Cloud Run verifies the Google-signed request before FastAPI handles it when the service
  is private and `chat@system.gserviceaccount.com` is the invoker.
- The event's Workspace user is then authorized separately. Request authentication does
  not imply user permission.
- BigQuery credentials are read-only for data; Cloud SQL credentials own action state.
- The LLM cannot submit SQL, decide approval, change an existing plan, or invoke IAM tools.
- Group/entitlement operations are rejected unless their exact resource is present in the
  collected evidence.
- The requestor cannot approve their own action, even if they also hold an admin role.
- Audit rows capture creation, decision, execution, and verification.
