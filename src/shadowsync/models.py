"""Typed data contracts shared by deterministic ShadowSync tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class SourceSystem(StrEnum):
    EXCEL = "excel"
    POWER_BI = "power_bi"
    SQL_SOR = "sql_sor"


class DriftType(StrEnum):
    STALE_PRICE = "stale_price"
    ORPHANED_ALLOCATION = "orphaned_allocation"
    QUANTITY_MISMATCH = "quantity_mismatch"
    UOM_INCONSISTENCY = "uom_inconsistency"
    MISSING_ATTRIBUTE = "missing_attribute"
    MISSING_SOURCE_RECORD = "missing_source_record"
    UNEXPECTED_SOURCE_RECORD = "unexpected_source_record"
    FIELD_MISMATCH = "field_mismatch"


class AuthoritativeSide(StrEnum):
    SOURCE = "source"
    SOR = "sor"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class NormalizedRow:
    """A normalized row with a stable table/key identity."""

    source: SourceSystem
    table: str
    key: str
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Drift:
    """One deterministic difference between an analyst source and SQL."""

    source: SourceSystem
    table: str
    key: str
    field: str
    source_value: Any
    sor_value: Any
    drift_type: DriftType
    sor_row_version: int | None

    @property
    def drift_id(self) -> str:
        return f"{self.source}:{self.table}:{self.key}:{self.field}"


@dataclass(frozen=True, slots=True)
class Classification:
    drift_type: DriftType
    authoritative_side: AuthoritativeSide
    confidence: float
    rule: str


@dataclass(frozen=True, slots=True)
class Resolution:
    authoritative_side: AuthoritativeSide
    proposed_value: Any
    rationale: str
    resolver: str


@dataclass(frozen=True, slots=True)
class ProposedChange:
    change_id: str
    drift_id: str
    table: str
    key: str
    key_field: str
    field: str
    old_value: Any
    new_value: Any
    expected_row_version: int


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_set_id: str
    source: SourceSystem
    created_at: str
    changes: tuple[ProposedChange, ...]
    dry_run_text: str
    dry_run_shown: bool = True


@dataclass(frozen=True, slots=True)
class ApprovalGate:
    change_set_id: str
    actor_id: str
    approved_at: str
    max_changes: int
    approval_token: str


@dataclass(frozen=True, slots=True)
class ApplyResult:
    change_set_id: str
    status: str
    applied_count: int
    skipped_count: int
    actor_id: str


@dataclass(frozen=True, slots=True)
class OutcomeMetrics:
    drift_rows_open: int
    drift_fields_open: int
    manual_baseline_minutes: float
    governed_reconciliation_minutes: float | None
    time_improvement_percent: float | None
