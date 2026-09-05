"""Gemini agent that grounds IAM conclusions in retrieved policy chunks."""

from iam_agent.agents.base import BaseAgent
from iam_agent.models import PolicyAssessment


class PolicyAgent(BaseAgent):
    name = "policy_rag"
    response_model = PolicyAssessment
    system_instruction = """
You are an IAM policy analyst. Use only policy chunks in the supplied payload.
Applicable controls must cite the exact supplied chunk_id as source_chunk_id. Do not
claim a policy requirement when no chunk supports it. Return policy analysis only.
""".strip()
