"""Small governed plans for unambiguous common requests; other questions use Gemini."""

from __future__ import annotations

import re

from iam_agent.models import AnalysisPlan, QueryStep


def risk_ranking_plan(
    question: str, catalog_context: dict, maximum_rows: int
) -> AnalysisPlan | None:
    text = re.sub(r"[^a-z0-9\s]", " ", question.casefold())
    words = set(text.split())
    allowed = set(
        "which who are is the me show give list rank return users identities user identity "
        "riskiest most risky highest risk score scores please top first and why "
        "export report reports as in csv excel xlsx pdf word docx all formats".split()
    )
    requested_limit = re.search(r"\b(?:top|first)\s+(\d+)\b", text)
    if requested_limit:
        words.discard(requested_limit.group(1))
    # Never discard an unfamiliar constraint such as a department, identity, date, or app.
    if words - allowed or not (
        "riskiest" in words or {"highest", "risk"} <= words or {"most", "risky"} <= words
    ):
        return None
    concept = next(
        (item for item in catalog_context.get("concepts", []) if item["concept"] == "risk_scores"),
        None,
    )
    if concept is None or "maximum_risk_score" not in concept.get("metrics", {}):
        return None
    if any(
        "risk_scores" not in item["concepts"]
        for item in catalog_context.get("evidence_requirements", [])
    ):
        return None
    limit = min(int(requested_limit.group(1)) if requested_limit else 10, maximum_rows)
    if limit < 1:
        return None
    explain = "why" in words
    if explain and "risk_factors" not in concept.get("selectable_fields", {}):
        return None
    return AnalysisPlan(
        objective="Rank identities by their highest stored risk scores across retained history",
        steps=[
            QueryStep(
                step_id="risk_ranking",
                purpose="Return highest observed risk scores, not a claim about present-day risk",
                concept="risk_scores",
                operation="records" if explain else "rank",
                fields=concept["default_fields"] if explain else [],
                dimensions=[] if explain else ["user"],
                metrics=[] if explain else ["maximum_risk_score"],
                order_by=[
                    {"field": "risk_score" if explain else "maximum_risk_score"},
                    {"field": "user", "direction": "ascending"},
                ],
                limit=limit,
            )
        ],
    )
