"""Gemini agent that evaluates the blast radius of remediation."""

from iam_agent.agents.base import BaseAgent
from iam_agent.models import ImpactAssessment


class ImpactAnalysisAgent(BaseAgent):
    name = "impact_analysis"
    response_model = ImpactAssessment
    system_instruction = """
You are an IAM impact analyst. Evaluate operational impact, blast radius, urgency,
and the risks of acting or not acting using only the supplied evidence. Do not select
or execute a remediation operation and do not assume facts absent from the evidence.
""".strip()
