"""Google Chat event parsing and cards for investigation approvals."""

from __future__ import annotations

import hashlib
import html
import json
from typing import Any

from iam_agent.authorization import AuthorizationError
from iam_agent.execution import ApprovalGateway
from iam_agent.models import ActionRecord, AgentIntent, ChatActor, IntentType, WorkflowResult
from iam_agent.orchestrator import IAMOrchestrator, UnsafeRecommendationError
from iam_agent.repository import ActionNotFound, InvalidTransition

HELP_TEXT = (
    "IAM Agent commands:\n"
    "• `investigate user12` — collect BigQuery evidence and propose a reviewed action\n"
    "• `status <action-id>` — show an action's current state\n"
    "All changes require a different authorized human approver."
)


def _actor(event: dict[str, Any]) -> ChatActor:
    user = event.get("user") or event.get("message", {}).get("sender") or {}
    principal = user.get("email") or user.get("name")
    if not principal:
        raise AuthorizationError("Google Chat user identity is missing")
    return ChatActor(
        principal=principal,
        display_name=user.get("displayName", ""),
        chat_user_name=user.get("name", ""),
    )


def _parameters(event: dict[str, Any]) -> dict[str, str]:
    common = event.get("common", {})
    if isinstance(common.get("parameters"), dict):
        return {str(key): str(value) for key, value in common["parameters"].items()}
    parameters = event.get("action", {}).get("parameters", [])
    return {str(item.get("key")): str(item.get("value")) for item in parameters}


def _status_card(action: ActionRecord, *, update: bool = False) -> dict[str, Any]:
    plan = action.recommendation
    target_user = html.escape(action.target_user)
    operation = html.escape(plan.operation.value)
    target_system = html.escape(plan.target_system.value)
    target_resource = html.escape(plan.target_resource)
    rationale = html.escape(plan.rationale)
    widgets: list[dict[str, Any]] = [
        {
            "decoratedText": {
                "topLabel": "Action ID",
                "text": action.action_id,
                "bottomLabel": f"Status: {action.status.value}",
            }
        },
        {
            "textParagraph": {
                "text": (
                    f"<b>Identity:</b> {target_user}<br>"
                    f"<b>Operation:</b> {operation}<br>"
                    f"<b>System:</b> {target_system}<br>"
                    f"<b>Resource:</b> {target_resource}<br>"
                    f"<b>Reason:</b> {rationale}"
                )
            }
        },
    ]
    if action.status.value == "PENDING_APPROVAL":
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        {
                            "text": "Approve",
                            "onClick": {
                                "action": {
                                    "function": "approve_action",
                                    "parameters": [{"key": "action_id", "value": action.action_id}],
                                }
                            },
                        },
                        {
                            "text": "Reject",
                            "onClick": {
                                "action": {
                                    "function": "reject_action",
                                    "parameters": [{"key": "action_id", "value": action.action_id}],
                                }
                            },
                        },
                    ]
                }
            }
        )
    response: dict[str, Any] = {
        "cardsV2": [
            {
                "cardId": f"iam-action-{action.action_id}",
                "card": {
                    "header": {"title": "IAM remediation review"},
                    "sections": [{"widgets": widgets}],
                },
            }
        ]
    }
    if update:
        response["actionResponse"] = {"type": "UPDATE_MESSAGE"}
    return response


def _no_action_card(result: WorkflowResult) -> dict[str, Any]:
    return {
        "text": (
            f"Investigation complete for {result.evidence.target_user}. "
            f"No IAM change was proposed. {result.recommendation.rationale}"
        )
    }


class GoogleChatHandler:
    def __init__(self, orchestrator: IAMOrchestrator, approvals: ApprovalGateway):
        self.orchestrator = orchestrator
        self.approvals = approvals

    async def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("type") or event.get("eventType", "")
        if event_type == "ADDED_TO_SPACE":
            return {"text": "IAM Agent is ready. Send `help` to see safe commands."}
        if event_type == "REMOVED_FROM_SPACE":
            return {}
        try:
            actor = _actor(event)
            if event_type == "MESSAGE":
                message = event.get("message", {})
                text = message.get("argumentText") or message.get("text", "")
                intent: AgentIntent = await self.orchestrator.intent_router.route(text)
                if intent.intent == IntentType.HELP:
                    return {"text": HELP_TEXT}
                if intent.intent == IntentType.UNKNOWN:
                    return {"text": "I couldn't identify a safe IAM command.\n" + HELP_TEXT}
                if intent.intent == IntentType.STATUS:
                    action = await self.orchestrator.get_status(actor, intent.action_id or "")
                    return _status_card(action)
                result = await self.orchestrator.investigate(
                    actor=actor,
                    target_user=intent.target_user or "",
                    source_event_id=message.get("name")
                    or hashlib.sha256(
                        json.dumps(event, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    chat_space=event.get("space", {}).get("name", ""),
                    chat_thread=message.get("thread", {}).get("name", ""),
                    original_request=text,
                )
                return _status_card(result.action) if result.action else _no_action_card(result)

            if event_type == "CARD_CLICKED":
                function_name = event.get("common", {}).get("invokedFunction") or event.get(
                    "action", {}
                ).get("actionMethodName")
                if function_name not in {"approve_action", "reject_action"}:
                    return {"text": "Unsupported approval action."}
                action_id = _parameters(event).get("action_id", "")
                result = await self.approvals.decide(
                    action_id=action_id,
                    approver=actor.principal,
                    approve=function_name == "approve_action",
                    reason="Decision submitted in Google Chat",
                )
                return _status_card(result.action, update=True)
            return {"text": "Unsupported Google Chat event."}
        except AuthorizationError:
            return {"text": "You are not authorized for this identity or action."}
        except ActionNotFound:
            return {"text": "The requested action was not found."}
        except InvalidTransition:
            return {"text": "That action has already been decided or executed."}
        except UnsafeRecommendationError:
            return {
                "text": (
                    "The recommendation failed the deterministic safety checks; "
                    "no action was queued."
                )
            }
        except ValueError:
            return {"text": "I couldn't validate that IAM request.\n" + HELP_TEXT}
