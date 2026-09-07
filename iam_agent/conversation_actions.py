"""Conversation-level actions over saved results, before domain and query planning."""

from __future__ import annotations

import re

from iam_agent.actions import ActionResolver, ReportAuthor
from iam_agent.conversation import ConversationStore
from iam_agent.email_service import EmailError, EmailService
from iam_agent.errors import ModelOutputError
from iam_agent.models import (
    AnalystAnswer,
    AnswerMode,
    ChatWorkflowResult,
    ConversationTurn,
    QuestionDecomposition,
    ReportDocument,
    ReportFormat,
    ReportSection,
)
from iam_agent.reporting import ReportService
from iam_agent.result_store import ResultStore


class ConversationActions:
    def __init__(
        self, *, resolver: ActionResolver, author: ReportAuthor, results: ResultStore,
        reports: ReportService, emails: EmailService, conversations: ConversationStore,
    ):
        self.resolver, self.author, self.results = resolver, author, results
        self.reports, self.emails, self.conversations = reports, emails, conversations

    @staticmethod
    def reply(text: str, question: str, source: ChatWorkflowResult | None = None):
        if source:
            reply = source.model_copy(deep=True)
            reply.answer = AnalystAnswer(answer=text)
            reply.reports = []
            return reply
        return ChatWorkflowResult(
            decomposition=QuestionDecomposition(
                answer_mode=AnswerMode.GENERAL, clarified_question=question,
                requires_project_data=False,
            ), answer=AnalystAnswer(answer=text),
        )

    async def render(self, owner, key, source, formats, instruction):
        if not source.report_document or re.search(
            r"\b(full|detailed|executive|technical|management|professional)\b", instruction, re.I,
        ):
            try:
                source.report_document = await self.author.write(source, instruction)
            except ModelOutputError:
                source.report_document = ReportDocument(
                    title="IAM Analytics Report", executive_summary=source.answer.answer,
                    sections=[ReportSection(heading="Report scope", body=(
                        "The expanded report writer was unavailable. This file preserves the "
                        "original analysis and returned evidence."
                    ))],
                )
        descriptors, failures = await self.reports.create_reports(
            owner=owner, result=source, formats=formats,
        )
        source.reports.extend(descriptors)
        await self.results.save(owner, key, source)
        return descriptors, failures

    async def prepare(
        self, *, owner: str, key: str, question: str, history: list[ConversationTurn],
        formats: list[ReportFormat], source_result_id: str,
    ) -> tuple[str, list[ReportFormat], ChatWorkflowResult | None]:
        snapshots = await self.results.list(owner, key)
        decision = await self.resolver.resolve(question, history, snapshots, source_result_id)
        formats = list(dict.fromkeys(formats + decision.formats))
        source_id = decision.source_result_id or source_result_id
        source = next((r for r in snapshots if r.result_id == source_id), None)
        if not source_id and snapshots:
            source = snapshots[-1]
        if decision.action == "analyze":
            return decision.resolved_question, formats, None
        if decision.action == "clarify":
            response = self.reply(
                decision.clarification or "Which earlier result do you mean?", question,
            )
        elif decision.action in {"export", "explain"} and source is None:
            response = self.reply(
                "I don't have that saved analysis. Please run the analysis first.", question,
            )
        elif decision.action == "export":
            descriptors, failures = await self.render(
                owner, key, source, formats or [ReportFormat.EXCEL], question,
            )
            response = self.reply(
                "Your report is ready from the saved analysis. Download it below."
                if descriptors else "I could not generate the requested report. Please try again.",
                question, source,
            )
            response.reports = descriptors
            if failures:
                response.answer.caveats = ["Unavailable formats: " + ", ".join(failures)]
        elif decision.action == "explain":
            # New contributing-event evidence must use analyze; explain uses only saved facts.
            document = await self.author.write(source, question)
            response = self.reply(document.executive_summary, question, source)
            response.answer.key_findings = [s.body for s in document.sections][:8]
            response.answer.sources = source.answer.sources
            response.answer.caveats = source.answer.caveats
        elif decision.action == "email":
            try:
                response = await self._email(owner, key, question, source, snapshots, decision)
            except EmailError as exc:
                response = self.reply(str(exc), question, source)
        else:
            return question, formats, None
        await self.conversations.append(key, ConversationTurn(
            user_message=question, assistant_summary=response.answer.answer,
            entities=source.decomposition.entities if source else [],
            domains=source.decomposition.domains if source else [],
        ))
        return question, formats, response

    async def _email(self, owner, key, question, source, snapshots, decision):
        available = [report for result in snapshots for report in result.reports]
        selected = list(decision.attachment_ids)
        previous = bool(re.search(
            r"\b(previous|earlier|already|produced|gave)\b", question, re.I,
        ))
        for report_format in decision.formats:
            matching = [r for r in available if r.format == report_format]
            if matching and previous:
                selected.append(matching[-1].report_id)
                source = next(r for r in reversed(snapshots) if matching[-1] in r.reports)
            elif source:
                current = [r for r in source.reports if r.format == report_format]
                if current:
                    selected.append(current[-1].report_id)
                elif previous:
                    raise EmailError(
                        "I couldn't find that previous file. Generate it first, then attach it.",
                    )
                else:
                    generated, failures = await self.render(
                        owner, key, source, [report_format], question,
                    )
                    if failures:
                        raise EmailError(
                            "The requested attachment could not be generated. Please try again.",
                        )
                    available.extend(generated)
                    selected.extend(r.report_id for r in generated)
            else:
                raise EmailError(
                    "Please run an analysis or generate the requested attachment first.",
                )
        if re.search(r"\battach\b", question, re.I) and not selected:
            if source and source.reports:
                selected = [source.reports[-1].report_id]
            else:
                raise EmailError(
                    "Which file should I attach? Generate a report first so I can use "
                    "its exact contents.",
                )
        content = await self.author.email(source, question)
        draft = await self.emails.create(
            owner=owner, conversation_key=key, content=content, recipient=decision.recipient,
            available=available, selected_ids=selected,
        )
        response = self.reply(
            "I've prepared an email draft. Enter the recipient's email address, check the "
            "message and attachments, then choose Preview email. Nothing has been sent.",
            question, source,
        )
        response.email_draft = draft
        return response
