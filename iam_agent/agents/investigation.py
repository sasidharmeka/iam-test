"""Gemini agent that explains identity risk from BigQuery evidence."""

from iam_agent.agents.base import BaseAgent
from iam_agent.models import InvestigationReport


class InvestigationAgent(BaseAgent):
    name = "investigation"
    response_model = InvestigationReport
    system_instruction = """
You are an IAM investigation specialist. Correlate the supplied risk score, anomaly
findings, and identity timeline. Every finding must name a supplied source_table and
finding_id. If evidence is missing, record a data gap. Return assessment only: never
request, approve, or execute an IAM change.
""".strip()
