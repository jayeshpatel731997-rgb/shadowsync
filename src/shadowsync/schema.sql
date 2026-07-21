PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS open_orders (
    order_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    vendor_id TEXT NOT NULL,
    quantity_each INTEGER NOT NULL CHECK (quantity_each >= 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'HOLD', 'CLOSED')),
    expected_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version > 0)
);

CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY,
    on_hand_each INTEGER NOT NULL CHECK (on_hand_each >= 0),
    uom TEXT NOT NULL CHECK (uom IN ('EA')),
    warehouse_zone TEXT,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version > 0)
);

CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES open_orders(order_id),
    sku TEXT NOT NULL REFERENCES inventory(sku),
    allocated_each INTEGER NOT NULL CHECK (allocated_each >= 0),
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version > 0)
);

CREATE TABLE IF NOT EXISTS vendor_terms (
    vendor_id TEXT PRIMARY KEY,
    vendor_name TEXT NOT NULL,
    payment_terms TEXT NOT NULL,
    lead_time_days INTEGER NOT NULL CHECK (lead_time_days >= 0),
    contact_email TEXT,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version > 0)
);

-- Reserved now so every future governed action has an append-only destination.
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TABLE IF NOT EXISTS governance_config (
    config_key TEXT PRIMARY KEY,
    config_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_sets (
    change_set_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    dry_run_text TEXT NOT NULL,
    dry_run_shown INTEGER NOT NULL CHECK (dry_run_shown = 1)
);

CREATE TABLE IF NOT EXISTS approval_gates (
    change_set_id TEXT PRIMARY KEY REFERENCES change_sets(change_set_id),
    actor_id TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    max_changes INTEGER NOT NULL CHECK (max_changes > 0),
    token_payload_json TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED'))
);

CREATE TABLE IF NOT EXISTS applied_changes (
    change_id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(change_set_id),
    applied_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value_json TEXT NOT NULL,
    new_value_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS change_sets_no_update
BEFORE UPDATE ON change_sets BEGIN SELECT RAISE(ABORT, 'change_sets is append-only'); END;
CREATE TRIGGER IF NOT EXISTS change_sets_no_delete
BEFORE DELETE ON change_sets BEGIN SELECT RAISE(ABORT, 'change_sets is append-only'); END;
CREATE TRIGGER IF NOT EXISTS applied_changes_no_update
BEFORE UPDATE ON applied_changes BEGIN SELECT RAISE(ABORT, 'applied_changes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS applied_changes_no_delete
BEFORE DELETE ON applied_changes BEGIN SELECT RAISE(ABORT, 'applied_changes is append-only'); END;
