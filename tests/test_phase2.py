from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from shadowsync.drift import classify_drift, detect_drift
from shadowsync.ingest import IngestionError, ingest_bi_extract, ingest_excel, read_all_sql_sor, read_sql_sor
from shadowsync.models import AuthoritativeSide, DriftType, SourceSystem
from shadowsync.seed import generate_demo


@pytest.fixture()
def demo(tmp_path: Path) -> dict[str, Path]:
    return generate_demo(tmp_path / "demo")


def test_typed_ingestion_normalizes_nulls_integers_and_sources(demo: dict[str, Path]) -> None:
    excel = ingest_excel(demo["excel"])
    bi = ingest_bi_extract(demo["bi_extract"])
    sor = read_all_sql_sor(demo["database"])

    assert {row.source for row in excel} == {SourceSystem.EXCEL}
    assert {row.source for row in bi} == {SourceSystem.POWER_BI}
    assert {row.source for row in sor} == {SourceSystem.SQL_SOR}
    blank_contact = next(row for row in excel if row.table == "vendor_terms" and row.key == "V-002")
    assert blank_contact.values["contact_email"] is None
    assert isinstance(next(row for row in excel if row.table == "open_orders").values["quantity_each"], int)


def test_excel_drift_detects_all_required_types_without_metadata_noise(demo: dict[str, Path]) -> None:
    excel = ingest_excel(demo["excel"])
    sor = read_all_sql_sor(demo["database"])
    drift = detect_drift(excel, sor)

    assert {item.drift_type for item in drift} == {
        DriftType.STALE_PRICE,
        DriftType.ORPHANED_ALLOCATION,
        DriftType.QUANTITY_MISMATCH,
        DriftType.UOM_INCONSISTENCY,
        DriftType.MISSING_ATTRIBUTE,
    }
    assert len(drift) == 5
    assert len({item.drift_id for item in drift}) == len(drift)
    assert all(item.field not in {"updated_at", "row_version"} for item in drift)


def test_bi_drift_is_source_specific_and_deterministic(demo: dict[str, Path]) -> None:
    bi = ingest_bi_extract(demo["bi_extract"])
    sor = read_sql_sor(demo["database"], "open_orders")
    first = detect_drift(bi, sor)
    second = detect_drift(reversed(bi), reversed(sor))
    assert first == second
    assert [(item.key, item.drift_type) for item in first] == [
        ("PO-1001", DriftType.STALE_PRICE),
        ("PO-1003", DriftType.QUANTITY_MISMATCH),
    ]
    assert all(item.source is SourceSystem.POWER_BI for item in first)


def test_classification_does_not_invent_authority_for_conflicting_values(demo: dict[str, Path]) -> None:
    classified = {
        item.drift_type: classify_drift(item)
        for item in detect_drift(ingest_excel(demo["excel"]), read_all_sql_sor(demo["database"]))
    }
    assert classified[DriftType.STALE_PRICE].authoritative_side is AuthoritativeSide.AMBIGUOUS
    assert classified[DriftType.QUANTITY_MISMATCH].authoritative_side is AuthoritativeSide.AMBIGUOUS
    assert classified[DriftType.ORPHANED_ALLOCATION].authoritative_side is AuthoritativeSide.SOR
    assert classified[DriftType.UOM_INCONSISTENCY].authoritative_side is AuthoritativeSide.SOR
    assert classified[DriftType.MISSING_ATTRIBUTE].authoritative_side is AuthoritativeSide.SOR
    assert all(item.authoritative_side is not AuthoritativeSide.SOURCE for item in classified.values())
    assert all(item.rule for item in classified.values())


def test_parquet_bi_extract_is_supported(demo: dict[str, Path], tmp_path: Path) -> None:
    frame = pd.read_csv(demo["bi_extract"])
    parquet_path = tmp_path / "power_bi_extract.parquet"
    frame.to_parquet(parquet_path, index=False)
    assert ingest_bi_extract(parquet_path) == ingest_bi_extract(demo["bi_extract"])


def test_duplicate_keys_and_invalid_sql_table_fail_closed(demo: dict[str, Path], tmp_path: Path) -> None:
    frame = pd.read_csv(demo["bi_extract"])
    frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    duplicate_path = tmp_path / "duplicate.csv"
    frame.to_csv(duplicate_path, index=False)
    with pytest.raises(IngestionError, match="duplicate key"):
        ingest_bi_extract(duplicate_path)
    with pytest.raises(IngestionError, match="unsupported SQL table"):
        read_sql_sor(demo["database"], "open_orders; DROP TABLE open_orders")


def test_missing_sheet_fails_without_partial_results(demo: dict[str, Path], tmp_path: Path) -> None:
    path = tmp_path / "incomplete.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"order_id": ["PO-1"]}).to_excel(writer, sheet_name="open_orders", index=False)
    with pytest.raises(IngestionError, match="missing required sheets"):
        ingest_excel(path)
