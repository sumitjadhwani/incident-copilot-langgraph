-- Application schema. Applied by scripts/db_migrate.py as the owner role.
-- LangGraph's checkpointer and store tables are created by their own setup() calls.

-- ---------------------------------------------------------------------------------------------
-- Audit log: append-only and hash-chained.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    tenant_id       TEXT        NOT NULL,
    thread_id       TEXT        NOT NULL,
    checkpoint_id   TEXT,
    approver_id     TEXT        NOT NULL,
    status          TEXT        NOT NULL CHECK (status IN ('approved', 'rejected', 'executed', 'failed', 'blocked')),
    action_tool     TEXT        NOT NULL,
    action_payload  JSONB       NOT NULL,
    action_hash     TEXT        NOT NULL,
    trace_id        TEXT,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    prev_hash       TEXT        NOT NULL,
    row_hash        TEXT        NOT NULL UNIQUE
);

-- Idempotency: one decision (approve OR reject) and at most one execution per action.
CREATE UNIQUE INDEX IF NOT EXISTS audit_one_decision_per_action
    ON audit_log (thread_id, action_hash) WHERE status IN ('approved', 'rejected');
CREATE UNIQUE INDEX IF NOT EXISTS audit_one_execution_per_action
    ON audit_log (thread_id, action_hash) WHERE status = 'executed';
CREATE INDEX IF NOT EXISTS audit_by_tenant ON audit_log (tenant_id, id);
CREATE INDEX IF NOT EXISTS audit_by_thread ON audit_log (thread_id, id);

-- Defense in depth: even the owner cannot rewrite history without dropping the trigger first.
CREATE OR REPLACE FUNCTION audit_log_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change();

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change();

-- ---------------------------------------------------------------------------------------------
-- Incident threads: ownership and tenancy for authorization checks.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incident_threads (
    thread_id       TEXT PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    owner_id        TEXT        NOT NULL,
    owner_username  TEXT        NOT NULL,
    scenario_id     TEXT,
    title           TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_threads_by_tenant ON incident_threads (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------------------------
-- Privileges for the application role.
-- ---------------------------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO copilot_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO copilot_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO copilot_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM copilot_app;
