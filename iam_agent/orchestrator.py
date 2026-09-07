"""Catalog-first planning, deterministic coverage, and grounded IAM answers."""

from __future__ import annotations

import logging

from iam_agent.answerer import AnswerAgent
from iam_agent.authorization import AuthorizationError, AuthorizationService
from iam_agent.config import Settings
from iam_agent.conversation import ConversationStore
from iam_agent.errors import DependencyError, ModelOutputError, WorkflowError
from iam_agent.mcp_client import IAMEvidenceMCPClient, MCPToolError
from iam_agent.models import ChatActor, ChatWorkflowResult, ConversationTurn, IAMDomain, ReportFormat
from iam_agent.planner import QuestionPlanner
from iam_agent.policy_rag import PolicyStore
from iam_agent.reporting import ReportService

logger = logging.getLogger(__name__)


class IAMOrchestrator:
    """Domain labels guide explanation; they never gate which evidence can be planned."""

    def __init__(
        self, *, settings: Settings, authorization: AuthorizationService,
        policy_store: PolicyStore, mcp_client: IAMEvidenceMCPClient,
        planner: QuestionPlanner, answerer: AnswerAgent, conversations: ConversationStore,
        report_service: ReportService | None = None,
    ):
        self.settings = settings
        self.authorization = authorization
        self.policy_store = policy_store
        self.mcp_client = mcp_client
        self.planner = planner
        self.answerer = answerer
        self.conversations = conversations
        self.report_service = report_service

    async def answer(
        self, *, actor: ChatActor, question: str, conversation_key: str,
        report_formats: list[ReportFormat] | None = None,
    ) -> ChatWorkflowResult:
        stage = "authorization"
        try:
            self.authorization.require_query(actor.principal)
            history = await self.conversations.get(conversation_key)
            stage = "catalog_retrieval"
            # The raw question reaches deterministic catalog retrieval BEFORE any Gemini call.
            retrieval = await self.mcp_client.search_catalog(actor.principal, question, history)
            domain_summaries = await self.mcp_client.domain_summaries()
            stage = "question_understanding"
            decomposition = await self.planner.decompose(
                question=question, history=history, domain_summaries=domain_summaries,
                retrieval=retrieval,
            )
            policy_chunks = (
                self._policy_chunks(question) if decomposition.requires_policy_context else []
            )
            plan = None
            evidence = None
            if decomposition.requires_project_data:
                context = dict(retrieval.catalog_context)
                context["evidence_requirements"] = [
                    item.model_dump(mode="json") for item in retrieval.requirements
                ]
                context["requested_entities"] = retrieval.entities
                limits = {
                    "maximum_steps": self.settings.query_max_steps,
                    "maximum_rows": self.settings.query_max_rows,
                    "maximum_lookback_days": self.settings.query_max_lookback_days,
                }
                for attempt in range(2):
                    stage = "query_planning"
                    try:
                        if attempt == 0:
                            plan = await self.planner.create_plan(
                                question=question, decomposition=decomposition, history=history,
                                catalog_context=context, **limits,
                            )
                        else:
                            plan = await self.planner.repair_plan(
                                question=question, decomposition=decomposition, history=history,
                                catalog_context=context, rejected_plan=plan,
                                rejection_reason=rejection_reason, **limits,
                            )
                        stage = "evidence_query"
                        evidence = await self.mcp_client.run_query(
                            actor.principal, plan, question=question, history=history,
                        )
                        break
                    except (ModelOutputError, MCPToolError, ValueError) as exc:
                        if isinstance(exc, MCPToolError) and exc.code != "plan_rejected":
                            raise
                        if attempt == 1:
                            raise
                        rejection_reason = str(exc)
                        logger.info("Repairing rejected analysis plan; reason=%s", rejection_reason)
                        # A wrong-domain plan must not be repaired against the same narrow menu.
                        context = await self.mcp_client.describe_domains(list(IAMDomain))
                        context["evidence_requirements"] = [
                            item.model_dump(mode="json") for item in retrieval.requirements
                        ]
                        context["requested_entities"] = retrieval.entities
                stage = "answer_generation"
                answer = await self.answerer.answer_from_evidence(
                    question=question, decomposition=decomposition, plan=plan, evidence=evidence,
                    history=history, policy_chunks=policy_chunks,
                )
            else:
                stage = "answer_generation"
                answer = await self.answerer.answer_general(
                    question=question, history=history, policy_chunks=policy_chunks,
                )

            await self.conversations.append(
                conversation_key,
                ConversationTurn(
                    user_message=question, assistant_summary=answer.answer,
                    entities=decomposition.entities, domains=decomposition.domains,
                ),
            )
            result = ChatWorkflowResult(
                decomposition=decomposition, plan=plan, evidence=evidence, answer=answer,
            )
            stage = "report_generation"
            if report_formats and self.report_service:
                result.reports, failures = await self.report_service.create_reports(
                    owner=actor.principal, result=result, formats=report_formats,
                )
                if failures and len(result.answer.caveats) < 8:
                    result.answer.caveats.append(
                        "Could not generate these requested report formats: " + ", ".join(failures)
                    )
            return result
        except AuthorizationError:
            raise
        except Exception as exc:
            if isinstance(exc, DependencyError):
                error = WorkflowError(stage, exc.code, str(exc))
            elif isinstance(exc, ValueError):
                error = WorkflowError(
                    stage, "plan_unresolved",
                    "I could not create an analysis that satisfies the evidence requirements. "
                    "Please specify the user, topic, or comparison you need.",
                )
            else:
                error = WorkflowError(
                    stage, "unexpected_error", "The analysis encountered an unexpected service error."
                )
            logger.exception(
                "IAM request failed reference=%s stage=%s code=%s",
                error.reference, error.stage, error.code,
            )
            raise error from exc

    def _policy_chunks(self, question: str) -> list[dict]:
        return [
            {"chunk_id": chunk.chunk_id, "source": chunk.source, "text": chunk.text}
            for chunk in self.policy_store.retrieve(question)
        ]
