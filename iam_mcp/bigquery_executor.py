"""Bounded, read-only execution of compiler-produced BigQuery queries."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from iam_agent.config import Settings
from iam_agent.errors import DependencyError
from iam_agent.models import CompiledQuery, QueryParameter, QueryStep, QueryStepResult

logger = logging.getLogger(__name__)


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
            raise DependencyError(
                "bigquery_budget",
                f"Step {step.step_id!r} would process {bytes_processed} bytes; "
                f"limit is {self._settings.query_max_bytes_billed}",
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
        from google.api_core import exceptions

        try:
            return await asyncio.to_thread(self._execute_sync, compiled, step)
        except exceptions.Forbidden as exc:
            logger.exception("BigQuery permission failure for concept=%s", step.concept)
            raise DependencyError(
                "bigquery_permission",
                "BigQuery denied access. Check the runtime service account's dataset read "
                "permission and BigQuery Job User role in the query project.",
            ) from exc
        except exceptions.NotFound as exc:
            logger.exception("BigQuery source missing for concept=%s", step.concept)
            raise DependencyError(
                "bigquery_not_found",
                f"The {step.concept} output was not found. "
                "Check the dataset/project and pipeline run.",
            ) from exc
        except exceptions.BadRequest as exc:
            logger.exception("BigQuery rejected compiled query for concept=%s", step.concept)
            raise DependencyError(
                "bigquery_schema",
                f"The {step.concept} query did not match the live BigQuery schema. "
                "Check Cloud Run logs for the missing column or type mismatch.",
            ) from exc
        except (
            exceptions.TooManyRequests,
            exceptions.ServiceUnavailable,
            exceptions.DeadlineExceeded,
        ) as exc:
            logger.exception("BigQuery unavailable for concept=%s", step.concept)
            raise DependencyError(
                "bigquery_unavailable", "BigQuery is temporarily unavailable or quota-limited."
            ) from exc
