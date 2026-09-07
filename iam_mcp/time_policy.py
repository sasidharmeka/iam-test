"""Use all retained history unless the user explicitly requests a date constraint."""

from __future__ import annotations

import re

from iam_agent.models import AnalysisPlan, ConversationTurn
from iam_mcp.catalog import SemanticCatalog

ALL_HISTORY = re.compile(
    r"\b(all (?:time|history|dates)|any time|without (?:a )?time limit)\b", re.I
)
EXPLICIT_TIME = re.compile(
    r"\b(?:19|20)\d{2}\b|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|"
    r"\b(?:last|past|previous|this|next)\s+(?:(?:\d+|one|two|three|six|twelve)\s+)?"
    r"(?:hours?|days?|weeks?|months?|quarters?|years?)\b|"
    r"\b(?:today|yesterday|tomorrow|january|february|march|april|june|july|august|"
    r"september|october|november|december|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b|\b(?:in|during)\s+may\b|\bmay\s+\d{1,2}\b|"
    r"\b\d+\s+(?:hours?|days?|weeks?|months?|years?)\s+ago\b",
    re.I,
)
TIME_FOLLOWUP = re.compile(r"\b(?:same (?:period|range)|that (?:period|range)|those dates)\b", re.I)


def has_requested_time_range(question: str, history: list[ConversationTurn]) -> bool:
    if ALL_HISTORY.search(question):
        return False
    if EXPLICIT_TIME.search(question):
        return True
    if TIME_FOLLOWUP.search(question) and history:
        return has_requested_time_range(history[-1].user_message, history[:-1])
    return False


class QueryTimePolicy:
    def __init__(self, catalog: SemanticCatalog):
        self.catalog = catalog

    def apply(
        self, plan: AnalysisPlan, question: str, history: list[ConversationTurn]
    ) -> tuple[AnalysisPlan, str]:
        if has_requested_time_range(question, history):
            return plan, "Explicit time scope: see the applied filters on each evidence result."
        normalized = plan.model_copy(deep=True)
        for step in normalized.steps:
            concept = self.catalog.concept(step.concept)
            time_fields = {
                name
                for name, field in concept.fields.items()
                if field.type in {"date", "timestamp"}
            }
            step.time_range_days = None
            step.filters = [item for item in step.filters if item.field not in time_fields]
        return normalized, (
            "Time scope: all retained dates; no rolling date cutoff was applied. "
            "Results describe stored historical data, which may not reflect current access."
        )
