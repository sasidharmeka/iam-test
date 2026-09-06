# Demo IAM analytical policy

## IAM-01 Least privilege

Privileged access without an approved request, unused access older than 45 days, and access
that no longer matches an identity's role should be investigated. Findings are signals for
human review; they are not proof of malicious behavior by themselves.

## IAM-02 Authentication anomalies

Impossible travel, MFA bypass, repeated authentication-method drift, and logins followed by
application activity outside a plausible interval require timely review. Correlate multiple
signals and the identity timeline before drawing a conclusion.

## IAM-03 Directory and entitlement anomalies

Shadow access, privilege escalation without a request, and execution after rejection are
high-priority governance findings. Evaluate the exact identity, resource, initiating actor,
request status, and event time recorded in the analytical output.

## IAM-04 Evidence handling

Project-specific statements must identify the BigQuery analytical output on which they are
based. Empty or missing evidence must be reported as a data gap. Never infer intent from a
risk score alone.

## IAM-05 Read-only boundary

The analytics agent may explain, compare, rank, summarize, and retrieve IAM evidence. It must
not create an approval, enqueue an action, mutate an identity system, or claim that a change
was executed. Any future remediation system must be a separately authorized control plane.
