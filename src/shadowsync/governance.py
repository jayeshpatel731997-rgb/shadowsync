"""Approval-gated, idempotent reconciliation tools for Phase 3."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd

from shadowsync.drift import classify_drift, detect_drift
from shadowsync.ingest import TABLE_SPECS, ingest_bi_extract, ingest_excel, read_all_sql_sor, read_sql_sor
from shadowsync.models import (
    ApplyResult,
    ApprovalGate,
    AuthoritativeSide,
    ChangeSet,
    Drift,
    OutcomeMetrics,
    ProposedChange,
    Resolution,
    SourceSystem,
)
from shadowsync.seed import connect_database

MAX_APPROVAL_BATCH = 25


class GovernanceError(RuntimeError):
    """Base class for fail-closed governance errors."""


class ApprovalRequiredError(GovernanceError):
    pass


class ConcurrencyConflictError(GovernanceError):
    pass


class InvalidChangeSetError(GovernanceError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _jsonable_change(change: ProposedChange) -> dict[str, Any]:
    return asdict(change)


def resolve_ambiguous_drift(
    drift: Drift,
    context: Mapping[str, Any] | None = None,
    resolver: Callable[[Drift, Mapping[str, Any]], Resolution] | None = None,
) -> Resolution:
    """Propose a resolution for an ambiguous drift; never approve or write it.

    ``resolver`` is the only extension point intended for structured AI. With no
    resolver/key, independent analyst-source values are counted deterministically;
    a tie defaults safely to SQL.
    """
    classification = classify_drift(drift)
    if classification.authoritative_side is not AuthoritativeSide.AMBIGUOUS:
        raise GovernanceError("only ambiguous drift may be sent to a resolver")
    safe_context = dict(context or {})
    if resolver is not None:
        resolution = resolver(drift, safe_context)
        if not isinstance(resolution, Resolution):
            raise GovernanceError("resolver returned an invalid structured result")
        if resolution.authoritative_side is AuthoritativeSide.AMBIGUOUS:
            raise GovernanceError("resolver must propose source or SOR")
        return resolution

    peer_values = safe_context.get("peer_source_values", ())
    if not isinstance(peer_values, (list, tuple)):
        raise GovernanceError("peer_source_values must be a list or tuple")
    source_support = 1 + sum(value == drift.source_value for value in peer_values)
    sor_support = sum(value == drift.sor_value for value in peer_values)
    side = AuthoritativeSide.SOURCE if source_support > sor_support and source_support >= 2 else AuthoritativeSide.SOR
    value = drift.source_value if side is AuthoritativeSide.SOURCE else drift.sor_value
    rationale = f"deterministic cross-source support source={source_support}, sor={sor_support}; selected {side.value}"
    return Resolution(side, value, rationale, "deterministic_fallback")


def propose_writeback(
    drift: Iterable[Drift],
    resolutions: Mapping[str, Resolution] | None = None,
    *,
    created_at: str | None = None,
) -> ChangeSet:
    """Build an immutable dry-run; row inserts/deletes are deliberately excluded."""
    items = sorted(drift, key=lambda item: item.drift_id)
    if not items:
        raise InvalidChangeSetError("cannot propose an empty drift collection")
    if len({item.drift_id for item in items}) != len(items):
        raise InvalidChangeSetError("drift collection contains duplicate drift IDs")
    sources = {item.source for item in items}
    if len(sources) != 1:
        raise InvalidChangeSetError("a change set must be bounded to one source system")
    resolution_map = dict(resolutions or {})
    changes: list[ProposedChange] = []
    for item in items:
        classification = classify_drift(item)
        side = classification.authoritative_side
        proposed_value = item.source_value
        if side is AuthoritativeSide.AMBIGUOUS:
            resolution = resolution_map.get(item.drift_id)
            if resolution is None:
                continue
            side = resolution.authoritative_side
            proposed_value = resolution.proposed_value
        if side is not AuthoritativeSide.SOURCE or item.field == "__row__":
            continue
        if item.sor_row_version is None:
            continue
        spec = TABLE_SPECS[item.table]
        identity = _canonical_json(
            [item.drift_id, item.sor_row_version, item.sor_value, proposed_value]
        )
        change_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        changes.append(
            ProposedChange(
                change_id,
                item.drift_id,
                item.table,
                item.key,
                spec.key,
                item.field,
                item.sor_value,
                proposed_value,
                item.sor_row_version,
            )
        )
    if not changes:
        raise InvalidChangeSetError("no SQL write-backs were proposed")
    if len(changes) > MAX_APPROVAL_BATCH:
        raise InvalidChangeSetError(f"change set exceeds maximum batch of {MAX_APPROVAL_BATCH}")
    canonical = _canonical_json([_jsonable_change(change) for change in changes])
    change_set_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    lines = [f"DRY RUN — {len(changes)} SQL field update(s)"]
    lines.extend(
        f"{change.table}[{change.key}].{change.field}: {change.old_value!r} -> {change.new_value!r} (expected row_version={change.expected_row_version})"
        for change in changes
    )
    return ChangeSet(change_set_id, next(iter(sources)), created_at or _utc_now(), tuple(changes), "\n".join(lines), True)


def _ensure_signing_secret(connection: sqlite3.Connection) -> bytes:
    row = connection.execute(
        "SELECT config_value FROM governance_config WHERE config_key = 'approval_signing_secret'"
    ).fetchone()
    if row is None:
        secret = secrets.token_hex(32)
        connection.execute(
            "INSERT INTO governance_config(config_key, config_value) VALUES ('approval_signing_secret', ?)",
            (secret,),
        )
        return bytes.fromhex(secret)
    return bytes.fromhex(row[0])


def _encode_token(payload_json: str, secret: bytes) -> str:
    payload = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _change_set_json(change_set: ChangeSet) -> str:
    return _canonical_json([_jsonable_change(change) for change in change_set.changes])


def _validate_change_set(change_set: ChangeSet) -> None:
    """Reject hand-crafted or corrupted change sets before approval or write."""
    if not change_set.changes:
        raise InvalidChangeSetError("change set cannot be empty")
    if len(change_set.changes) > MAX_APPROVAL_BATCH:
        raise InvalidChangeSetError(f"change set exceeds maximum batch of {MAX_APPROVAL_BATCH}")
    if len({change.change_id for change in change_set.changes}) != len(change_set.changes):
        raise InvalidChangeSetError("change set contains duplicate change IDs")
    if len({change.drift_id for change in change_set.changes}) != len(change_set.changes):
        raise InvalidChangeSetError("change set contains duplicate drift IDs")
    for change in change_set.changes:
        spec = TABLE_SPECS.get(change.table)
        writable = set(spec.fields) - {spec.key, "updated_at", "row_version"} if spec else set()
        if spec is None or change.key_field != spec.key or change.field not in writable:
            raise InvalidChangeSetError("change targets a non-allow-listed writable field")
        if change.expected_row_version < 1 or change.old_value == change.new_value:
            raise InvalidChangeSetError("change has an invalid version or no value difference")
        identity = _canonical_json(
            [change.drift_id, change.expected_row_version, change.old_value, change.new_value]
        )
        expected_change_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        if not hmac.compare_digest(change.change_id, expected_change_id):
            raise InvalidChangeSetError("change ID does not match its contents")
    expected_set_id = hashlib.sha256(_change_set_json(change_set).encode()).hexdigest()[:24]
    if not hmac.compare_digest(change_set.change_set_id, expected_set_id):
        raise InvalidChangeSetError("change set ID does not match its contents")


def emit_audit(
    database: Path,
    event_type: str,
    actor_id: str,
    payload: Mapping[str, Any],
    *,
    occurred_at: str | None = None,
    event_id: str | None = None,
) -> str:
    """Append one attributable audit event; duplicate IDs fail closed."""
    if not event_type.strip() or not actor_id.strip():
        raise GovernanceError("audit event_type and actor_id are required")
    identifier = event_id or str(uuid.uuid4())
    with connect_database(Path(database)) as connection:
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)",
            (identifier, occurred_at or _utc_now(), event_type, actor_id, _canonical_json(payload)),
        )
    return identifier


def request_approval(
    database: Path,
    change_set: ChangeSet,
    actor_id: str,
    *,
    approved_at: str | None = None,
    max_changes: int = MAX_APPROVAL_BATCH,
) -> ApprovalGate:
    """Persist an attributable approval bound to the exact shown dry-run."""
    _validate_change_set(change_set)
    if not actor_id.strip():
        raise GovernanceError("an attributable actor_id is required")
    if not change_set.dry_run_shown or not change_set.dry_run_text:
        raise GovernanceError("approval requires a shown dry-run")
    if not change_set.changes or len(change_set.changes) > min(max_changes, MAX_APPROVAL_BATCH):
        raise GovernanceError("change set exceeds the approval batch bound")
    timestamp = approved_at or _utc_now()
    changes_json = _change_set_json(change_set)
    with connect_database(Path(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        secret = _ensure_signing_secret(connection)
        existing = connection.execute(
            "SELECT actor_id, approved_at, max_changes, token_payload_json FROM approval_gates WHERE change_set_id = ?",
            (change_set.change_set_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != actor_id:
                raise GovernanceError("change set was already approved by another actor")
            token = _encode_token(existing[3], secret)
            return ApprovalGate(change_set.change_set_id, existing[0], existing[1], existing[2], token)
        connection.execute(
            "INSERT INTO change_sets VALUES (?, ?, ?, ?, ?, ?)",
            (change_set.change_set_id, change_set.created_at, change_set.source.value, changes_json, change_set.dry_run_text, 1),
        )
        payload_json = _canonical_json(
            {"change_set_id": change_set.change_set_id, "actor_id": actor_id, "approved_at": timestamp, "max_changes": max_changes}
        )
        token = _encode_token(payload_json, secret)
        connection.execute(
            "INSERT INTO approval_gates VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')",
            (change_set.change_set_id, actor_id, timestamp, max_changes, payload_json, hashlib.sha256(token.encode()).hexdigest()),
        )
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, 'approval_granted', ?, ?)",
            (str(uuid.uuid4()), timestamp, actor_id, _canonical_json({"change_set_id": change_set.change_set_id, "change_count": len(change_set.changes)})),
        )
    return ApprovalGate(change_set.change_set_id, actor_id, timestamp, max_changes, token)


def _verify_gate(connection: sqlite3.Connection, change_set: ChangeSet, token: str) -> tuple[str, str]:
    try:
        payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise ApprovalRequiredError("invalid approval token") from exc
    secret = _ensure_signing_secret(connection)
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ApprovalRequiredError("invalid approval token signature")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    gate = connection.execute(
        "SELECT actor_id, status, max_changes, token_hash FROM approval_gates WHERE change_set_id = ?",
        (change_set.change_set_id,),
    ).fetchone()
    if gate is None or not hmac.compare_digest(gate[3], token_hash):
        raise ApprovalRequiredError("approval token is not bound to this change set")
    if len(change_set.changes) > gate[2]:
        raise ApprovalRequiredError("change set exceeds approved bound")
    stored = connection.execute(
        "SELECT changes_json FROM change_sets WHERE change_set_id = ?", (change_set.change_set_id,)
    ).fetchone()
    if stored is None or not hmac.compare_digest(stored[0], _change_set_json(change_set)):
        raise ApprovalRequiredError("change set differs from the approved dry-run")
    return gate[0], gate[1]


def apply_writeback(
    database: Path,
    change_set: ChangeSet,
    approval_token: str,
    *,
    applied_at: str | None = None,
) -> ApplyResult:
    """Atomically apply a bounded batch with token, idempotency, and version checks."""
    _validate_change_set(change_set)
    timestamp = applied_at or _utc_now()
    with connect_database(Path(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        actor_id, gate_status = _verify_gate(connection, change_set, approval_token)
        applied_ids = {
            row[0]
            for row in connection.execute(
                "SELECT change_id FROM applied_changes WHERE change_set_id = ?", (change_set.change_set_id,)
            )
        }
        pending = [change for change in change_set.changes if change.change_id not in applied_ids]
        if not pending:
            return ApplyResult(change_set.change_set_id, "IDEMPOTENT_REPLAY", 0, len(applied_ids), actor_id)
        if gate_status != "ACTIVE":
            raise ApprovalRequiredError("approval gate is already consumed")

        grouped: dict[tuple[str, str], list[ProposedChange]] = defaultdict(list)
        for change in pending:
            spec = TABLE_SPECS.get(change.table)
            if spec is None or change.key_field != spec.key or change.field not in spec.fields:
                raise InvalidChangeSetError("change targets a non-allow-listed table or field")
            grouped[(change.table, change.key)].append(change)

        # Validate every row before the first mutation so concurrency failures are atomic.
        for (table, key), changes in grouped.items():
            expected_versions = {change.expected_row_version for change in changes}
            if len(expected_versions) != 1:
                raise InvalidChangeSetError("changes for one row disagree on expected row_version")
            spec = TABLE_SPECS[table]
            fields = [change.field for change in changes]
            row = connection.execute(
                f"SELECT {', '.join(fields)}, row_version FROM {table} WHERE {spec.key} = ?", (key,)
            ).fetchone()
            if row is None or row[-1] != next(iter(expected_versions)):
                raise ConcurrencyConflictError(f"stale row version for {table}/{key}")
            for index, change in enumerate(changes):
                if row[index] != change.old_value:
                    raise ConcurrencyConflictError(f"stale field value for {table}/{key}/{change.field}")

        for (table, key), changes in grouped.items():
            spec = TABLE_SPECS[table]
            assignments = [f"{change.field} = ?" for change in changes]
            assignments.extend(["updated_at = ?", "row_version = row_version + 1"])
            params = [change.new_value for change in changes] + [timestamp, key, changes[0].expected_row_version]
            cursor = connection.execute(
                f"UPDATE {table} SET {', '.join(assignments)} WHERE {spec.key} = ? AND row_version = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflictError(f"concurrent update for {table}/{key}")
            for change in changes:
                connection.execute(
                    "INSERT INTO applied_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (change.change_id, change_set.change_set_id, timestamp, actor_id, change.table, change.key, change.field, _canonical_json(change.old_value), _canonical_json(change.new_value)),
                )
        connection.execute(
            "UPDATE approval_gates SET status = 'CONSUMED' WHERE change_set_id = ?", (change_set.change_set_id,)
        )
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, 'writeback_applied', ?, ?)",
            (str(uuid.uuid4()), timestamp, actor_id, _canonical_json({"change_set_id": change_set.change_set_id, "applied_count": len(pending)})),
        )
    return ApplyResult(change_set.change_set_id, "APPLIED", len(pending), len(applied_ids), actor_id)


def observe_outcome(
    database: Path,
    excel_path: Path,
    bi_path: Path,
    manual_baseline_path: Path,
) -> OutcomeMetrics:
    """Compute deterministic open-drift and reconciliation-time KPIs."""
    sor_all = read_all_sql_sor(database)
    excel_drift = detect_drift(ingest_excel(excel_path), sor_all)
    bi_rows = ingest_bi_extract(bi_path)
    bi_tables = sorted({row.table for row in bi_rows})
    bi_sor = [row for table in bi_tables for row in read_sql_sor(database, table)]
    bi_drift = detect_drift(bi_rows, bi_sor)
    all_drift = excel_drift + bi_drift
    drift_rows = {(item.source, item.table, item.key) for item in all_drift}
    baseline = float(pd.read_csv(manual_baseline_path)["minutes_to_reconcile"].mean())
    with connect_database(Path(database)) as connection:
        timestamps = connection.execute(
            """SELECT a.approved_at, MIN(c.applied_at)
               FROM approval_gates a JOIN applied_changes c USING(change_set_id)
               GROUP BY a.change_set_id, a.approved_at"""
        ).fetchall()
    governed: float | None = None
    improvement: float | None = None
    if timestamps:
        durations = []
        for approved, applied in timestamps:
            start = datetime.fromisoformat(approved.replace("Z", "+00:00"))
            end = datetime.fromisoformat(applied.replace("Z", "+00:00"))
            durations.append(max((end - start).total_seconds() / 60, 0.0))
        governed = sum(durations) / len(durations)
        improvement = ((baseline - governed) / baseline) * 100 if baseline else None
    return OutcomeMetrics(len(drift_rows), len(all_drift), baseline, governed, improvement)


def escalate(database: Path, reason: str, actor_id: str = "shadowsync-orchestrator") -> str:
    """Append an escalation without changing operational data."""
    if not reason.strip():
        raise GovernanceError("escalation reason is required")
    return emit_audit(database, "drift_escalated", actor_id, {"reason": reason})
