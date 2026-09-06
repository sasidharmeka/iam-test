"""Bounded, read-only execution of compiler-produced BigQuery queries."""

from __future__ import annotations

import asyncio
from typing import Protocol

from iam_agent.config import Settings
from iam_agent.models import CompiledQuery, QueryParameter, QueryStep, QueryStepResult


class QueryExecutor(Protocol):
    async def execute(self, compiled: CompiledQuery, step: QueryStep) -> QueryStepResult: ...


class BigQueryExecutor:
    def __init__(self, settings: Settings, client: object | None = None):
        from google.cloud import bigquery

        self._bigquery = bigquery
        self._settings = settings
        self._client = client or bigquery.Client(project=settings.bigquery_project)

    def _query_parameters(self, parameters: list[QueryParameter]) -> list[object]:
        converted: list[object] = []
        for parameter in parameters:
            if parameter.is_array:
                converted.append(
                    self._bigquery.ArrayQueryParameter(
                        parameter.name, parameter.bigquery_type, parameter.value
                    )
                )
            else:
                converted.append(
                    self._bigquery.ScalarQueryParameter(
                        parameter.name, parameter.bigquery_type, parameter.value
                    )
                )
        return converted

    def _execute_sync(self, compiled: CompiledQuery, step: QueryStep) -> QueryStepResult:
        query_parameters = self._query_parameters(compiled.parameters)
        dry_run_config = self._bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            query_parameters=query_parameters,
        )
        dry_run_job = self._client.query(compiled.sql, job_config=dry_run_config)
        bytes_processed = int(dry_run_job.total_bytes_processed or 0)
        if bytes_processed > self._settings.query_max_bytes_billed:
            raise ValueError(
                f"Step {step.step_id!r} would process {bytes_processed} bytes; "
                f"limit is {self._settings.query_max_bytes_billed}"
            )

        job_config = self._bigquery.QueryJobConfig(
            maximum_bytes_billed=self._settings.query_max_bytes_billed,
            query_parameters=query_parameters,
            use_query_cache=True,
        )
        job = self._client.query(compiled.sql, job_config=job_config)
        rows = [dict(row.items()) for row in job.result()]
        return QueryStepResult(
            step_id=step.step_id,
            purpose=step.purpose,
            concept=step.concept,
            source_table=compiled.table,
            rows=rows,
            row_count=len(rows),
            bytes_processed=bytes_processed,
            truncated=len(rows) >= compiled.limit,
        )

    async def execute(self, compiled: CompiledQuery, step: QueryStep) -> QueryStepResult:
        return await asyncio.to_thread(self._execute_sync, compiled, step)
