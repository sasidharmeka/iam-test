"""Cloud SQL-compatible persistence for actions, audits, and simulated IAM state."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint, create_engine, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from iam_agent.models import ActionRecord, ActionStatus, utc_now


class RepositoryError(RuntimeError):
    pass


class ActionNotFound(RepositoryError):
    pass


class InvalidTransition(RepositoryError):
    pass


class Base(DeclarativeBase):
    pass


JSON_VALUE = JSON().with_variant(JSONB, "postgresql")


class ActionQueueRow(Base):
    __tablename__ = "action_queue"
    __table_args__ = (UniqueConstraint("source_event_id", name="uq_action_source_event"),)

    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requestor: Mapped[str] = mapped_column(String(320), nullable=False)
    target_user: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_scope: Mapped[str] = mapped_column(String(1024), nullable=False)
    evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    recommendation: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    chat_space: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    chat_thread: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    decision_by: Mapped[str | None] = mapped_column(String(320))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    audit_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SimulatedIAMStateRow(Base):
    __tablename__ = "simulated_iam_state"
    __table_args__ = (
        UniqueConstraint(
            "target_system", "target_user", "target_resource", name="uq_simulated_state"
        ),
    )

    state_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_system: Mapped[str] = mapped_column(String(64), nullable=False)
    target_user: Mapped[str] = mapped_column(String(255), nullable=False)
    target_resource: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SQLActionRepository:
    """Uses PostgreSQL in Cloud SQL and SQLite for local development/tests."""

    def __init__(self, database_url: str):
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @staticmethod
    def _to_record(row: ActionQueueRow) -> ActionRecord:
        return ActionRecord.model_validate(
            {
                "action_id": row.action_id,
                "status": row.status,
                "requestor": row.requestor,
                "target_user": row.target_user,
                "target_scope": row.target_scope,
                "evidence_snapshot": row.evidence_snapshot,
                "recommendation": row.recommendation,
                "analysis": row.analysis,
                "source_event_id": row.source_event_id,
                "chat_space": row.chat_space,
                "chat_thread": row.chat_thread,
                "decision_by": row.decision_by,
                "decision_reason": row.decision_reason,
                "execution_result": row.execution_result,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )

    def get_action(self, action_id: str) -> ActionRecord:
        with self._sessions() as session:
            row = session.get(ActionQueueRow, action_id)
            if row is None:
                raise ActionNotFound(action_id)
            return self._to_record(row)

    def get_by_source_event(self, source_event_id: str) -> ActionRecord | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ActionQueueRow).where(ActionQueueRow.source_event_id == source_event_id)
            )
            return self._to_record(row) if row else None

    def create_action(self, action: ActionRecord) -> ActionRecord:
        existing = self.get_by_source_event(action.source_event_id)
        if existing:
            return existing
        row = ActionQueueRow(
            action_id=action.action_id,
            status=action.status.value,
            requestor=action.requestor,
            target_user=action.target_user,
            target_scope=action.target_scope,
            evidence_snapshot=action.evidence_snapshot.model_dump(mode="json"),
            recommendation=action.recommendation.model_dump(mode="json"),
            analysis=action.analysis.model_dump(mode="json"),
            source_event_id=action.source_event_id,
            chat_space=action.chat_space,
            chat_thread=action.chat_thread,
            created_at=action.created_at,
            updated_at=action.updated_at,
        )
        try:
            with self._sessions.begin() as session:
                session.add(row)
                session.add(
                    AuditEventRow(
                        action_id=action.action_id,
                        stage="ACTION_CREATED",
                        actor=action.requestor,
                        payload={
                            "status": action.status.value,
                            "evidence_tables": action.evidence_snapshot.queried_tables,
                            "recommendation": action.recommendation.model_dump(mode="json"),
                        },
                    )
                )
        except IntegrityError:
            # Google Chat retries failed deliveries. A concurrent request may have
            # inserted the same source_event_id after our initial read.
            existing = self.get_by_source_event(action.source_event_id)
            if existing:
                return existing
            raise
        return self.get_action(action.action_id)

    def transition(
        self,
        action_id: str,
        *,
        expected: Iterable[ActionStatus],
        new_status: ActionStatus,
        values: dict[str, Any] | None = None,
    ) -> ActionRecord:
        expected_values = [status.value for status in expected]
        updates = {"status": new_status.value, "updated_at": utc_now(), **(values or {})}
        with self._sessions.begin() as session:
            result = session.execute(
                update(ActionQueueRow)
                .where(
                    ActionQueueRow.action_id == action_id,
                    ActionQueueRow.status.in_(expected_values),
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                current = session.get(ActionQueueRow, action_id)
                if current is None:
                    raise ActionNotFound(action_id)
                raise InvalidTransition(
                    f"Cannot move action {action_id} from {current.status} to {new_status.value}"
                )
        return self.get_action(action_id)

    def append_audit(self, action_id: str, stage: str, actor: str, payload: dict[str, Any]) -> None:
        with self._sessions.begin() as session:
            session.add(
                AuditEventRow(
                    action_id=action_id,
                    stage=stage,
                    actor=actor,
                    payload=payload,
                )
            )

    def list_audit(self, action_id: str) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.action_id == action_id)
                .order_by(AuditEventRow.audit_id)
            ).all()
            return [
                {
                    "stage": row.stage,
                    "actor": row.actor,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def get_simulated_state(
        self, target_system: str, target_user: str, target_resource: str
    ) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.scalar(
                select(SimulatedIAMStateRow).where(
                    SimulatedIAMStateRow.target_system == target_system,
                    SimulatedIAMStateRow.target_user == target_user,
                    SimulatedIAMStateRow.target_resource == target_resource,
                )
            )
            return dict(row.state) if row else None

    def set_simulated_state(
        self,
        target_system: str,
        target_user: str,
        target_resource: str,
        state: dict[str, Any],
    ) -> None:
        with self._sessions.begin() as session:
            row = session.scalar(
                select(SimulatedIAMStateRow).where(
                    SimulatedIAMStateRow.target_system == target_system,
                    SimulatedIAMStateRow.target_user == target_user,
                    SimulatedIAMStateRow.target_resource == target_resource,
                )
            )
            if row:
                row.state = state
                row.updated_at = utc_now()
            else:
                session.add(
                    SimulatedIAMStateRow(
                        target_system=target_system,
                        target_user=target_user,
                        target_resource=target_resource,
                        state=state,
                    )
                )
