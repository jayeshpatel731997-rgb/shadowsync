from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from shadowsync.seed import connect_database, ensure_database_schema, generate_demo


@pytest.fixture()
def demo(tmp_path: Path) -> dict[str, Path]:
    return generate_demo(tmp_path / "demo")


def test_database_has_expected_synthetic_records_and_constraints(demo: dict[str, Path]) -> None:
    with connect_database(demo["database"]) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("open_orders", "inventory", "allocations", "vendor_terms")
        }
        assert counts == {"open_orders": 3, "inventory": 3, "allocations": 2, "vendor_terms": 2}
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)",
            ("EV-TEST", "2026-07-21T14:00:00Z", "seeded", "test", "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE audit_events SET event_type = 'changed' WHERE event_id = 'EV-TEST'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_events WHERE event_id = 'EV-TEST'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO allocations VALUES (?, ?, ?, ?, ?, ?)",
                ("AL-BAD", "PO-MISSING", "SKU-A100", 1, "2026-07-21T14:00:00Z", 1),
            )


def test_excel_contains_all_five_seeded_drift_types(demo: dict[str, Path]) -> None:
    sheets = pd.read_excel(demo["excel"], sheet_name=None)
    assert sheets["open_orders"].set_index("order_id").loc["PO-1001", "unit_price_cents"] == 1195
    assert sheets["open_orders"].set_index("order_id").loc["PO-1002", "quantity_each"] == 60
    assert sheets["inventory"].set_index("sku").loc["SKU-A100", "uom"] == "Each"
    assert "AL-999" in set(sheets["allocations"]["allocation_id"])
    assert pd.isna(sheets["vendor_terms"].set_index("vendor_id").loc["V-002", "contact_email"])
    assert sheets["README"].set_index("property").loc["data_classification", "value"] == "SYNTHETIC"


def test_bi_extract_and_baseline_are_well_formed(demo: dict[str, Path]) -> None:
    bi = pd.read_csv(demo["bi_extract"])
    baseline = pd.read_csv(demo["manual_baseline"])
    assert set(bi["dataset"]) == {"open_orders"}
    assert bi.set_index("order_id").loc["PO-1003", "quantity_each"] == 70
    assert set(baseline["drift_type"]) == {
        "stale_price", "orphaned_allocation", "quantity_mismatch",
        "uom_inconsistency", "missing_attribute",
    }
    assert (baseline["minutes_to_reconcile"] > 0).all()


def test_generation_is_reproducible(tmp_path: Path) -> None:
    first = generate_demo(tmp_path / "first")
    second = generate_demo(tmp_path / "second")
    for key in ("bi_extract", "manual_baseline"):
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest(first[key]) == digest(second[key])
    assert pd.read_excel(first["excel"], sheet_name=None).keys() == pd.read_excel(second["excel"], sheet_name=None).keys()


def test_schema_upgrade_preserves_existing_data(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('preserve-me')")
    ensure_database_schema(database)
    with connect_database(database) as connection:
        assert connection.execute("SELECT value FROM legacy_marker").fetchone()[0] == "preserve-me"
        governance_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('change_sets','approval_gates','applied_changes')"
            )
        }
        assert governance_tables == {"change_sets", "approval_gates", "applied_changes"}
