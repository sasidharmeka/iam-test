"""Read-only, allowlisted access to IAM_NEW_1 BigQuery analytics tables."""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from iam_agent.config import Settings
from iam_agent.models import EvidenceBundle, EvidenceRecord

USER_SCOPED_TABLES: tuple[str, ...] = (
    "risk_scores",
    "identity_timelines",
    "impossible_login_anomalies",
    "mfa_bypass_events",
    "mfa_bypass_events_all_apps",
    "impossible_travel_events",
    "privilege_escalation_without_request",
    "rejected_but_executed",
    "high_risk_sequence",
    "excessive_data_transferred",
    "role_drift",
    "stale_access",
    "multi_app_single_session",
    "mfa_drift",
    "orphan_event_detection",
    "orphan_user_detection",
    "shadow_access_events",
    "suspicious_time_warp",
)


class EvidenceProvider(Protocol):
    async def collect(self, target_user: str) -> EvidenceBundle: ...


class BigQueryEvidenceProvider:
    """Queries fixed tables with named parameters; it cannot execute model-authored SQL."""

    def __init__(self, settings: Settings, client: object | None = None):
        from google.cloud import bigquery

        self._bigquery = bigquery
        self._project = settings.bigquery_project
        self._dataset = settings.bigquery_dataset
        self._rows_per_table = settings.bigquery_rows_per_table
        self._client = client or bigquery.Client(project=self._project)

    def _query_text(self) -> str:
        statements: list[str] = []
        for table in USER_SCOPED_TABLES:
            statements.append(
                "\n".join(
                    [
                        f"SELECT '{table}' AS source_table, row_json",
                        "FROM (",
                        "  SELECT TO_JSON_STRING(t) AS row_json",
                        f"  FROM `{self._project}.{self._dataset}.{table}` AS t",
                        "  WHERE t.user = @target_user",
                        "  ORDER BY t.detected_at DESC",
                        "  LIMIT @row_limit",
                        ")",
                    ]
                )
            )
        return "\nUNION ALL\n".join(statements)

    def _collect_sync(self, target_user: str) -> EvidenceBundle:
        job_config = self._bigquery.QueryJobConfig(
            query_parameters=[
                self._bigquery.ScalarQueryParameter("target_user", "STRING", target_user),
                self._bigquery.ScalarQueryParameter("row_limit", "INT64", self._rows_per_table),
            ]
        )
        rows = self._client.query(self._query_text(), job_config=job_config).result()
        grouped: dict[str, list[dict]] = {table: [] for table in USER_SCOPED_TABLES}
        for row in rows:
            table = row["source_table"]
            if table not in grouped:
                continue
            grouped[table].append(json.loads(row["row_json"]))

        findings = [
            EvidenceRecord(table=table, data=item)
            for table, table_rows in grouped.items()
            if table not in {"risk_scores", "identity_timelines"}
            for item in table_rows
        ]
        return EvidenceBundle(
            target_user=target_user,
            risk_profile=(grouped["risk_scores"] or [{}])[0],
            timeline=grouped["identity_timelines"],
            findings=findings,
            queried_tables=list(USER_SCOPED_TABLES),
        )

    async def collect(self, target_user: str) -> EvidenceBundle:
        return await asyncio.to_thread(self._collect_sync, target_user)
