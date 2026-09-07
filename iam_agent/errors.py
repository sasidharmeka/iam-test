"""Errors that preserve diagnostics without returning credentials, SQL, or tracebacks."""

from __future__ import annotations

from uuid import uuid4


class ModelOutputError(ValueError):
    """Structured generation finished but did not satisfy the application contract."""


class DependencyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class WorkflowError(RuntimeError):
    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.reference = uuid4().hex[:12]
