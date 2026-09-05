"""Gemini agent that proposes one constrained, reviewable execution plan."""

from iam_agent.agents.base import BaseAgent
from iam_agent.models import RemediationRecommendation


class RemediationAgent(BaseAgent):
    name = "remediation"
    response_model = RemediationRecommendation
    system_instruction = """
You are an IAM remediation planner. Select exactly one operation from the response
schema. Choose no_action when evidence does not justify a change. The target_user
must exactly match the supplied target_user. A plan is only a recommendation and
must be approved by a human; never claim it has run. Use the smallest blast-radius
operation supported by the investigation, policy, and impact assessments.
""".strip()
