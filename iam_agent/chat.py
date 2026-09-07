"""Google Chat and browser-event adapter for the read-only IAM analyst."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from iam_agent.authorization import AuthorizationError
from iam_agent.errors import WorkflowError
from iam_agent.mcp_client import MCPToolError
from iam_agent.models import ChatActor, ChatWorkflowResult
from iam_agent.orchestrator import IAMOrchestrator
from iam_agent.reporting import requested_report_formats
from iam_mcp.catalog import CatalogError
from iam_mcp.validator import QueryPlanError

HELP_TEXT = """IAM Analytics Agent is read-only. Ask natural-language questions such as:
• Which users have the highest risk scores?
• Why is user12 risky?
• Show MFA bypasses by application.
• Compare role drift across departments.
• What changed for user12 during the last 30 days?
• What does shadow access mean?
• Export the riskiest users as Excel and PDF.

The agent can query only the 22 governed IAM analytics outputs and never modifies IAM data."""

logger = logging.getLogger(__name__)


def _actor(event: dict[str, Any]) -> ChatActor:
    user = event.get("user") or event.get("message", {}).get("sender") or {}
    principal = user.get("email") or user.get("name")
    if not principal:
        raise AuthorizationError("Google user identity is missing")
    return ChatActor(
        principal=principal,
        display_name=user.get("displayName", ""),
        chat_user_name=user.get("name", ""),
    )


def _conversation_key(event: dict[str, Any], actor: ChatActor) -> str:
    message = event.get("message", {})
    space = event.get("space", {}).get("name", "direct")
    thread = message.get("thread", {}).get("name", "main")
    return f"{space}:{thread}:{actor.principal.casefold()}"


def _render(result: ChatWorkflowResult, *, suggestions: bool = True) -> str:
    answer = result.answer
    sections = [answer.answer.strip()]
    if answer.key_findings:
        sections.append("Key findings:\n" + "\n".join(f"• {item}" for item in answer.key_findings))
    if answer.sources:
        sections.append("Sources: " + ", ".join(f"`{source}`" for source in answer.sources))
    if answer.caveats:
        sections.append("Caveats:\n" + "\n".join(f"• {item}" for item in answer.caveats))
    if suggestions and answer.suggested_questions:
        sections.append(
            "You can also ask:\n" + "\n".join(f"• {item}" for item in answer.suggested_questions)
        )
    return "\n\n".join(section for section in sections if section)


class GoogleChatHandler:
    def __init__(self, orchestrator: IAMOrchestrator):
        self.orchestrator = orchestrator

    async def handle(
        self,
        event: dict[str, Any],
        *,
        include_reports: bool = False,
    ) -> dict[str, Any]:
        event_type = event.get("type") or event.get("eventType", "")
        if event_type == "ADDED_TO_SPACE":
            return {"text": "IAM Analytics Agent is ready. Send `help` or ask an IAM question."}
        if event_type == "REMOVED_FROM_SPACE":
            return {}
        if event_type != "MESSAGE":
            return {"text": "This read-only agent accepts messages only."}

        try:
            actor = _actor(event)
            message = event.get("message", {})
            text = (message.get("argumentText") or message.get("text", "")).strip()
            if not text or text.casefold() in {"help", "/help", "about"}:
                return {"text": HELP_TEXT}
            result = await self.orchestrator.answer(
                actor=actor,
                question=text,
                conversation_key=_conversation_key(event, actor),
                report_formats=requested_report_formats(text) if include_reports else [],
                source_result_id=message.get("source_result_id", "") if include_reports else "",
                allow_actions=include_reports,
            )
            response: dict[str, Any] = {"text": _render(result, suggestions=not include_reports)}
            if include_reports:
                response["result_id"] = result.result_id
                response["suggested_questions"] = result.answer.suggested_questions
                if result.email_draft:
                    response["email_draft"] = result.email_draft
            if include_reports and result.reports:
                response["reports"] = [report.model_dump(mode="json") for report in result.reports]
            return response
        except AuthorizationError:
            return {"text": "You are not authorized to query this IAM analytics environment."}
        except WorkflowError as exc:
            response = {"text": f"{exc} Reference: {exc.reference} ({exc.stage}, {exc.code})."}
            if include_reports:
                response["error"] = {
                    "reference": exc.reference,
                    "stage": exc.stage,
                    "code": exc.code,
                }
            return response
        except (CatalogError, QueryPlanError, MCPToolError, ValueError) as exc:
            reference = uuid4().hex[:12]
            logger.warning("Safe analytical request rejected [%s]: %s", reference, exc)
            return {
                "text": (
                    "I could not build a safe query for that request. "
                    f"Try narrowing or rephrasing it. Reference: {reference}"
                )
            }
        except Exception:
            reference = uuid4().hex[:12]
            logger.exception("Unexpected conversational request failure [%s]", reference)
            return {
                "text": (
                    "The request could not be completed because of an internal error. "
                    f"Please try again. Reference: {reference}"
                )
            }
