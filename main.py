"""Cloud Run entry point: ``uvicorn main:app``."""

from iam_agent.api import create_app

app = create_app()
