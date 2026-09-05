CREATE TABLE IF NOT EXISTS action_queue (
    action_id VARCHAR(36) PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    requestor VARCHAR(320) NOT NULL,
    target_user VARCHAR(255) NOT NULL,
    target_scope VARCHAR(1024) NOT NULL,
    evidence_snapshot JSONB NOT NULL,
    recommendation JSONB NOT NULL,
    analysis JSONB NOT NULL,
    source_event_id VARCHAR(512) NOT NULL UNIQUE,
    chat_space VARCHAR(512) NOT NULL DEFAULT '',
    chat_thread VARCHAR(512) NOT NULL DEFAULT '',
    decision_by VARCHAR(320),
    decision_reason TEXT,
    execution_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_action_queue_status ON action_queue (status);
CREATE INDEX IF NOT EXISTS ix_action_queue_target_user ON action_queue (target_user);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id BIGSERIAL PRIMARY KEY,
    action_id VARCHAR(36) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    actor VARCHAR(320) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_audit_events_action_id ON audit_events (action_id);

CREATE TABLE IF NOT EXISTS simulated_iam_state (
    state_id BIGSERIAL PRIMARY KEY,
    target_system VARCHAR(64) NOT NULL,
    target_user VARCHAR(255) NOT NULL,
    target_resource VARCHAR(512) NOT NULL,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_simulated_state UNIQUE (target_system, target_user, target_resource)
);
