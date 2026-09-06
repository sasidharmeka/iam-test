"""Coordinates Gemini planning, MCP evidence access, and grounded answers."""

from __future__ import annotations

from iam_agent.answerer import AnswerAgent
from iam_agent.authorization import AuthorizationService
from iam_agent.config import Settings
from iam_agent.conversation import ConversationStore
from iam_agent.mcp_client import IAMEvidenceMCPClient
from iam_agent.models import ChatActor, ChatWorkflowResult, ConversationTurn
from iam_agent.planner import QuestionPlanner
from iam_agent.policy_rag import PolicyStore


class IAMOrchestrator:
    """Read-only conversational loop; it owns no SQL and no mutation capability."""

    def __init__(
        self,
        *,
        settings: Settings,
        authorization: AuthorizationService,
        policy_store: PolicyStore,
        mcp_client: IAMEvidenceMCPClient,
        planner: QuestionPlanner,
        answerer: AnswerAgent,
        conversations: ConversationStore,
    ):
        self.settings = settings
        self.authorization = authorization
        self.policy_store = policy_store
        self.mcp_client = mcp_client
        self.planner = planner
        self.answerer = answerer
        self.conversations = conversations

    async def answer(
        self,
        *,
        actor: ChatActor,
        question: str,
        conversation_key: str,
    ) -> ChatWorkflowResult:
        self.authorization.require_query(actor.principal)
        history = await self.conversations.get(conversation_key)
        domain_summaries = await self.mcp_client.domain_summaries()
        decomposition = await self.planner.decompose(
            question=question,
            history=history,
            domain_summaries=domain_summaries,
        )
        policy_chunks = (
            self._policy_chunks(question) if decomposition.requires_policy_context else []
        )

        plan = None
        evidence = None
        if decomposition.requires_project_data:
            catalog_context = await self.mcp_client.describe_domains(decomposition.domains)
            plan = await self.planner.create_plan(
                question=question,
                decomposition=decomposition,
                history=history,
                catalog_context=catalog_context,
                maximum_steps=self.settings.query_max_steps,
                maximum_rows=self.settings.query_max_rows,
                maximum_lookback_days=self.settings.query_max_lookback_days,
            )
            evidence = await self.mcp_client.run_query(actor.principal, plan)
            answer = await self.answerer.answer_from_evidence(
                question=question,
                decomposition=decomposition,
                plan=plan,
                evidence=evidence,
                history=history,
                policy_chunks=policy_chunks,
            )
        else:
            answer = await self.answerer.answer_general(
                question=question,
                history=history,
                policy_chunks=policy_chunks,
            )

        await self.conversations.append(
            conversation_key,
            ConversationTurn(
                user_message=question,
                assistant_summary=answer.answer,
                entities=decomposition.entities,
                domains=decomposition.domains,
            ),
        )
        return ChatWorkflowResult(
            decomposition=decomposition,
            plan=plan,
            evidence=evidence,
            answer=answer,
        )

    def _policy_chunks(self, question: str) -> list[dict]:
        return [
            {"chunk_id": chunk.chunk_id, "source": chunk.source, "text": chunk.text}
            for chunk in self.policy_store.retrieve(question)
        ]
