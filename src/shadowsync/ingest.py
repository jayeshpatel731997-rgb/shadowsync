"""Deterministic ingestion and normalization tools."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

import pandas as pd

from shadowsync.models import NormalizedRow, SourceSystem
from shadowsync.seed import connect_database


class IngestionError(ValueError):
    """Raised when an input cannot be safely ingested."""


@dataclass(frozen=True, slots=True)
class TableSpec:
    key: str
    fields: tuple[str, ...]
    integer_fields: tuple[str, ...]


TABLE_SPECS: Final[Mapping[str, TableSpec]] = {
    "open_orders": TableSpec(
        "order_id",
        ("order_id", "sku", "vendor_id", "quantity_each", "unit_price_cents", "status", "expected_date", "updated_at", "row_version"),
        ("quantity_each", "unit_price_cents", "row_version"),
    ),
    "inventory": TableSpec(
        "sku",
        ("sku", "on_hand_each", "uom", "warehouse_zone", "updated_at", "row_version"),
        ("on_hand_each", "row_version"),
    ),
    "allocations": TableSpec(
        "allocation_id",
        ("allocation_id", "order_id", "sku", "allocated_each", "updated_at", "row_version"),
        ("allocated_each", "row_version"),
    ),
    "vendor_terms": TableSpec(
        "vendor_id",
        ("vendor_id", "vendor_name", "payment_terms", "lead_time_days", "contact_email", "updated_at", "row_version"),
        ("lead_time_days", "row_version"),
    ),
}


def _clean_scalar(value: Any) -> Any:
    """Collapse spreadsheet nulls and trim strings without changing meaning."""
    if value is None or (not isinstance(value, (list, dict, tuple)) and pd.isna(value)):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def _normalize_frame(frame: pd.DataFrame, table: str, source: SourceSystem) -> list[NormalizedRow]:
    if table not in TABLE_SPECS:
        raise IngestionError(f"unsupported table: {table}")
    spec = TABLE_SPECS[table]
    missing = set(spec.fields) - set(frame.columns)
    if missing:
        raise IngestionError(f"{table} missing required columns: {', '.join(sorted(missing))}")

    normalized: list[NormalizedRow] = []
    seen: set[str] = set()
    for row_number, raw in enumerate(frame.loc[:, spec.fields].to_dict("records"), start=2):
        values = {field: _clean_scalar(raw[field]) for field in spec.fields}
        for field in spec.integer_fields:
            value = values[field]
            if value is None:
                raise IngestionError(f"{table} row {row_number}: {field} cannot be blank")
            try:
                numeric = float(value)
                if not numeric.is_integer():
                    raise ValueError
                values[field] = int(numeric)
            except (TypeError, ValueError, OverflowError) as exc:
                raise IngestionError(f"{table} row {row_number}: {field} must be an integer") from exc
        key_value = values[spec.key]
        if key_value is None:
            raise IngestionError(f"{table} row {row_number}: {spec.key} cannot be blank")
        key = str(key_value).strip().upper()
        values[spec.key] = key
        if key in seen:
            raise IngestionError(f"{table} contains duplicate key: {key}")
        seen.add(key)
        normalized.append(NormalizedRow(source=source, table=table, key=key, values=values))
    return sorted(normalized, key=lambda row: row.key)


def ingest_excel(path: Path) -> list[NormalizedRow]:
    """Read all governed sheets from an XLSX workbook.

    Failure behavior: missing/unreadable files, missing sheets/columns, invalid integer
    cells, and duplicate keys raise ``IngestionError`` without returning partial data.
    """
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".xlsx":
        raise IngestionError(f"expected an existing .xlsx file: {path}")
    try:
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except Exception as exc:
        raise IngestionError(f"could not read Excel workbook: {path}") from exc
    missing_sheets = set(TABLE_SPECS) - set(sheets)
    if missing_sheets:
        raise IngestionError(f"workbook missing required sheets: {', '.join(sorted(missing_sheets))}")
    return [row for table in TABLE_SPECS for row in _normalize_frame(sheets[table], table, SourceSystem.EXCEL)]


def ingest_bi_extract(path: Path) -> list[NormalizedRow]:
    """Read a simulated Power BI CSV or parquet dataset."""
    path = Path(path)
    if not path.is_file() or path.suffix.lower() not in {".csv", ".parquet"}:
        raise IngestionError(f"expected an existing .csv or .parquet file: {path}")
    try:
        frame = pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_parquet(path)
    except Exception as exc:
        raise IngestionError(f"could not read BI extract: {path}") from exc
    if "dataset" not in frame.columns:
        raise IngestionError("BI extract missing required dataset column")
    datasets = {_clean_scalar(value) for value in frame["dataset"]}
    if len(datasets) != 1 or None in datasets:
        raise IngestionError("BI extract must contain exactly one nonblank dataset")
    table = str(next(iter(datasets))).strip().lower()
    return _normalize_frame(frame.drop(columns="dataset"), table, SourceSystem.POWER_BI)


def read_sql_sor(path: Path, table: str) -> list[NormalizedRow]:
    """Read one allow-listed SQLite table as normalized rows."""
    if table not in TABLE_SPECS:
        raise IngestionError(f"unsupported SQL table: {table}")
    path = Path(path)
    if not path.is_file():
        raise IngestionError(f"SQLite database does not exist: {path}")
    try:
        with connect_database(path) as connection:
            frame = pd.read_sql_query(f"SELECT * FROM {table}", connection)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise IngestionError(f"could not read SQL table: {table}") from exc
    return _normalize_frame(frame, table, SourceSystem.SQL_SOR)


def read_all_sql_sor(path: Path) -> list[NormalizedRow]:
    return [row for table in TABLE_SPECS for row in read_sql_sor(path, table)]
