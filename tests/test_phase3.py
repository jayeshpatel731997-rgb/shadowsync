from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from shadowsync.drift import detect_drift
from shadowsync.governance import (
    ApprovalRequiredError,
    ConcurrencyConflictError,
    GovernanceError,
    InvalidChangeSetError,
    apply_writeback,
    escalate,
    observe_outcome,
    propose_writeback,
    request_approval,
    resolve_ambiguous_drift,
)
from shadowsync.ingest import ingest_bi_extract, ingest_excel, read_all_sql_sor, read_sql_sor
from shadowsync.models import AuthoritativeSide, DriftType, Resolution
from shadowsync.seed import connect_database, generate_demo


@pytest.fixture()
def demo(tmp_path: Path) -> dict[str, Path]:
    return generate_demo(tmp_path / "demo")


def _source_change_set(demo: dict[str, Path]):
    drift = detect_drift(ingest_excel(demo["excel"]), read_all_sql_sor(demo["database"]))
    bi_rows = ingest_bi_extract(demo["bi_extract"])
    peers = {(row.table, row.key): row for row in bi_rows}
    ambiguous = [item for item in drift if item.drift_type in {DriftType.STALE_PRICE, DriftType.QUANTITY_MISMATCH}]
    resolutions = {}
    for item in ambiguous:
        peer = peers.get((item.table, item.key))
        peer_values = [] if peer is None else [peer.values[item.field]]
        resolutions[item.drift_id] = resolve_ambiguous_drift(item, {"peer_source_values": peer_values})
    return propose_writeback(drift, resolutions, created_at="2026-07-21T15:00:00Z")


def _all_drift_ids(demo: dict[str, Path]) -> set[str]:
    excel = detect_drift(ingest_excel(demo["excel"]), read_all_sql_sor(demo["database"]))
    bi = detect_drift(ingest_bi_extract(demo["bi_extract"]), read_sql_sor(demo["database"], "open_orders"))
    return {item.drift_id for item in excel + bi}


def test_no_key_fallback_is_deterministic_and_ai_is_confined(demo: dict[str, Path]) -> None:
    drift = detect_drift(ingest_excel(demo["excel"]), read_all_sql_sor(demo["database"]))
    ambiguous = next(item for item in drift if item.drift_type is DriftType.STALE_PRICE)
    assert resolve_ambiguous_drift(ambiguous) == resolve_ambiguous_drift(ambiguous)
    assert resolve_ambiguous_drift(ambiguous).authoritative_side is AuthoritativeSide.SOR
    corroborated = resolve_ambiguous_drift(ambiguous, {"peer_source_values": [ambiguous.source_value]})
    assert corroborated.authoritative_side is AuthoritativeSide.SOURCE
    non_ambiguous = next(item for item in drift if item.drift_type is DriftType.ORPHANED_ALLOCATION)
    called = False

    def resolver(*_args):
        nonlocal called
        called = True
        return Resolution(AuthoritativeSide.SOURCE, non_ambiguous.source_value, "bad", "mock")

    with pytest.raises(GovernanceError, match="only ambiguous"):
        resolve_ambiguous_drift(non_ambiguous, resolver=resolver)
    assert called is False


def test_dry_run_is_bounded_and_shows_exact_changes(demo: dict[str, Path]) -> None:
    change_set = _source_change_set(demo)
    assert len(change_set.changes) == 1
    assert change_set.dry_run_shown
    assert "PO-1001" in change_set.dry_run_text
    assert "1195" in change_set.dry_run_text
    assert {change.field for change in change_set.changes} == {"unit_price_cents"}


def test_writeback_requires_attributable_approval_and_reduces_drift(demo: dict[str, Path]) -> None:
    before = observe_outcome(demo["database"], demo["excel"], demo["bi_extract"], demo["manual_baseline"])
    before_ids = _all_drift_ids(demo)
    change_set = _source_change_set(demo)
    with pytest.raises(ApprovalRequiredError):
        apply_writeback(demo["database"], change_set, "not-a-token")

    gate = request_approval(
        demo["database"], change_set, "analyst@example.invalid",
        approved_at="2026-07-21T15:01:00Z", max_changes=1,
    )
    result = apply_writeback(
        demo["database"], change_set, gate.approval_token, applied_at="2026-07-21T15:03:00Z"
    )
    after = observe_outcome(demo["database"], demo["excel"], demo["bi_extract"], demo["manual_baseline"])
    after_ids = _all_drift_ids(demo)

    assert result.status == "APPLIED"
    assert result.actor_id == "analyst@example.invalid"
    assert result.applied_count == 1
    assert after.drift_rows_open < before.drift_rows_open
    assert after.drift_fields_open == before.drift_fields_open - 2
    assert after_ids < before_ids  # reconciliation closes drift without moving it to another source
    assert after.governed_reconciliation_minutes == 2.0
    assert after.time_improvement_percent is not None and after.time_improvement_percent > 0


def test_writeback_is_idempotent(demo: dict[str, Path]) -> None:
    change_set = _source_change_set(demo)
    gate = request_approval(demo["database"], change_set, "analyst-1")
    first = apply_writeback(demo["database"], change_set, gate.approval_token)
    replay = apply_writeback(demo["database"], change_set, gate.approval_token)
    assert first.applied_count == 1
    assert replay.status == "IDEMPOTENT_REPLAY"
    assert replay.applied_count == 0
    with connect_database(demo["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM applied_changes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='writeback_applied'").fetchone()[0] == 1


def test_optimistic_concurrency_aborts_whole_batch(demo: dict[str, Path]) -> None:
    change_set = _source_change_set(demo)
    gate = request_approval(demo["database"], change_set, "analyst-1")
    with connect_database(demo["database"]) as connection:
        connection.execute("UPDATE open_orders SET unit_price_cents=1249, row_version=2 WHERE order_id='PO-1001'")
    with pytest.raises(ConcurrencyConflictError, match="stale"):
        apply_writeback(demo["database"], change_set, gate.approval_token)
    with connect_database(demo["database"]) as connection:
        assert connection.execute("SELECT unit_price_cents FROM open_orders WHERE order_id='PO-1001'").fetchone()[0] == 1249
        assert connection.execute("SELECT COUNT(*) FROM applied_changes").fetchone()[0] == 0


def test_tampering_and_oversized_approval_fail_closed(demo: dict[str, Path]) -> None:
    change_set = _source_change_set(demo)
    with pytest.raises(GovernanceError, match="batch bound"):
        request_approval(demo["database"], change_set, "analyst-1", max_changes=0)
    gate = request_approval(demo["database"], change_set, "analyst-1", max_changes=1)
    first = change_set.changes[0]
    tampered = replace(change_set, changes=(replace(first, new_value=1), *change_set.changes[1:]))
    with pytest.raises(InvalidChangeSetError, match="ID does not match"):
        apply_writeback(demo["database"], tampered, gate.approval_token)


def test_malformed_change_set_cannot_receive_approval(demo: dict[str, Path]) -> None:
    change_set = _source_change_set(demo)
    first = change_set.changes[0]
    key_mutation = replace(first, field=first.key_field, new_value="PO-HIJACKED")
    malformed = replace(change_set, changes=(key_mutation, *change_set.changes[1:]))
    with pytest.raises(InvalidChangeSetError, match="writable field"):
        request_approval(demo["database"], malformed, "analyst-1")

    duplicate = replace(change_set, changes=(first, first))
    with pytest.raises(InvalidChangeSetError, match="duplicate"):
        request_approval(demo["database"], duplicate, "analyst-1")


def test_audit_is_attributable_append_only_and_escalation_is_non_mutating(demo: dict[str, Path]) -> None:
    escalate(demo["database"], "unknown UOM mapping", actor_id="orchestrator")
    with connect_database(demo["database"]) as connection:
        event = connection.execute(
            "SELECT event_type, actor_id FROM audit_events WHERE event_type='drift_escalated'"
        ).fetchone()
        assert event == ("drift_escalated", "orchestrator")
        assert connection.execute("SELECT unit_price_cents FROM open_orders WHERE order_id='PO-1001'").fetchone()[0] == 1250
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_events")
