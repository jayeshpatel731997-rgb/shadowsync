"""Deterministic drift detection and classification."""

from __future__ import annotations

from typing import Any, Iterable

from shadowsync.ingest import TABLE_SPECS
from shadowsync.models import (
    AuthoritativeSide,
    Classification,
    Drift,
    DriftType,
    NormalizedRow,
    SourceSystem,
)

IGNORED_COMPARISON_FIELDS = frozenset({"updated_at", "row_version"})
QUANTITY_FIELDS = frozenset({"quantity_each", "on_hand_each", "allocated_each"})


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _type_for_field(table: str, field: str, source_value: Any, sor_value: Any) -> DriftType:
    if _is_blank(source_value) != _is_blank(sor_value):
        return DriftType.MISSING_ATTRIBUTE
    if table == "open_orders" and field == "unit_price_cents":
        return DriftType.STALE_PRICE
    if field in QUANTITY_FIELDS:
        return DriftType.QUANTITY_MISMATCH
    if field == "uom":
        return DriftType.UOM_INCONSISTENCY
    return DriftType.FIELD_MISMATCH


def _index(rows: Iterable[NormalizedRow]) -> dict[tuple[str, str], NormalizedRow]:
    index: dict[tuple[str, str], NormalizedRow] = {}
    for row in rows:
        identity = (row.table, row.key)
        if identity in index:
            raise ValueError(f"duplicate normalized identity: {row.table}/{row.key}")
        index[identity] = row
    return index


def detect_drift(source_rows: Iterable[NormalizedRow], sor_rows: Iterable[NormalizedRow]) -> list[Drift]:
    """Return stable, field-level drift records without mutating either side."""
    source_index = _index(source_rows)
    sor_index = _index(sor_rows)
    source_systems = {row.source for row in source_index.values()}
    if not source_systems or len(source_systems) != 1 or SourceSystem.SQL_SOR in source_systems:
        raise ValueError("source_rows must contain exactly one non-SQL source system")
    source_system = next(iter(source_systems))
    if any(row.source is not SourceSystem.SQL_SOR for row in sor_index.values()):
        raise ValueError("sor_rows must contain only SQL system-of-record rows")

    drift: list[Drift] = []
    all_identities = sorted(set(source_index) | set(sor_index))
    for table, key in all_identities:
        source = source_index.get((table, key))
        sor = sor_index.get((table, key))
        if source is None and sor is not None:
            drift.append(Drift(source_system, table, key, "__row__", None, dict(sor.values), DriftType.MISSING_SOURCE_RECORD, int(sor.values["row_version"])))
            continue
        if source is not None and sor is None:
            drift_type = DriftType.ORPHANED_ALLOCATION if table == "allocations" else DriftType.UNEXPECTED_SOURCE_RECORD
            drift.append(Drift(source_system, table, key, "__row__", dict(source.values), None, drift_type, None))
            continue
        assert source is not None and sor is not None
        spec = TABLE_SPECS[table]
        for field in spec.fields:
            if field == spec.key or field in IGNORED_COMPARISON_FIELDS:
                continue
            source_value = source.values[field]
            sor_value = sor.values[field]
            if source_value != sor_value:
                drift.append(
                    Drift(
                        source_system,
                        table,
                        key,
                        field,
                        source_value,
                        sor_value,
                        _type_for_field(table, field, source_value, sor_value),
                        int(sor.values["row_version"]),
                    )
                )
    return sorted(drift, key=lambda item: item.drift_id)


def classify_drift(drift: Drift) -> Classification:
    """Classify a drift using deterministic rules only."""
    if drift.drift_type in {
        DriftType.ORPHANED_ALLOCATION,
        DriftType.MISSING_ATTRIBUTE,
        DriftType.MISSING_SOURCE_RECORD,
    }:
        return Classification(drift.drift_type, AuthoritativeSide.SOR, 1.0, "sql_system_of_record")
    if drift.drift_type in {DriftType.STALE_PRICE, DriftType.QUANTITY_MISMATCH}:
        return Classification(drift.drift_type, AuthoritativeSide.AMBIGUOUS, 0.5, "conflicting_operational_values")
    if drift.drift_type is DriftType.UOM_INCONSISTENCY:
        source = str(drift.source_value).strip().upper()
        sor = str(drift.sor_value).strip().upper()
        aliases = {"EACH": "EA", "EACHES": "EA", "EA": "EA"}
        if aliases.get(source) == aliases.get(sor):
            return Classification(drift.drift_type, AuthoritativeSide.SOR, 1.0, "known_uom_alias")
        return Classification(drift.drift_type, AuthoritativeSide.AMBIGUOUS, 0.5, "unknown_uom_mapping")
    if drift.drift_type is DriftType.UNEXPECTED_SOURCE_RECORD:
        return Classification(drift.drift_type, AuthoritativeSide.AMBIGUOUS, 0.5, "unrecognized_source_record")
    return Classification(drift.drift_type, AuthoritativeSide.AMBIGUOUS, 0.5, "no_deterministic_authority_rule")
