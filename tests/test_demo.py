from pathlib import Path

from shadowsync.demo import run_demo
from shadowsync.governance import observe_outcome
from shadowsync.seed import connect_database


def test_demo_dry_run_does_not_write_without_approval(tmp_path: Path) -> None:
    output = tmp_path / "dry"
    assert run_demo(output) == 0
    with connect_database(output / "shadowsync.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM applied_changes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM approval_gates").fetchone()[0] == 0


def test_demo_approved_batch_reduces_open_drift_and_replay_is_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "approved"
    assert run_demo(output, "portfolio-reviewer") == 0
    metrics = observe_outcome(
        output / "shadowsync.db",
        output / "analyst_shadow.xlsx",
        output / "power_bi_extract.csv",
        output / "manual_baseline.csv",
    )
    assert metrics.drift_rows_open == 5
    with connect_database(output / "shadowsync.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM applied_changes").fetchone()[0] == 1
        assert connection.execute("SELECT actor_id FROM approval_gates").fetchone()[0] == "portfolio-reviewer"
