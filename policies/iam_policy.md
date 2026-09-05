# Demo IAM Security Policy

## IAM-01 Human approval for remediation

Every privilege removal, account suspension, MFA reset, or entitlement revocation
recommended by an automated system must be reviewed by an authorized human before
execution. The approved operation, target identity, target system, and target resource
must be identical to the plan shown to the approver.

## IAM-02 Least privilege

Privileged access without an approved request, unused access older than 45 days, and
access that no longer matches the identity's role should be investigated. Remediation
must use the narrowest operation that addresses the supported finding.

## IAM-03 Separation of duties

The person who requests or generates a remediation action must not approve that same
action. Approval authority is limited to the approver's assigned organizational scope.

## IAM-04 Evidence and auditability

An investigation must retain the analytical source, finding identifiers, policy
references, recommendation, human decision, execution result, and verification result.
BigQuery analytical evidence is read-only; mutable workflow state belongs in the
operational action queue.

## IAM-05 Authentication anomalies

Impossible travel, MFA bypass, and repeated authentication-method drift require prompt
investigation. Account-wide suspension is reserved for critical, corroborated evidence;
an MFA reset is preferred when it adequately contains the risk.

## IAM-06 Directory and entitlement anomalies

Shadow access, privilege escalation without a request, or execution after rejection must
be escalated for human review. A group or entitlement may be removed only when that exact
resource appears in the collected evidence for the target identity.
