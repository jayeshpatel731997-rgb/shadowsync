from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from shadowsync.api import create_app
from shadowsync.seed import connect_database


def test_api_governed_approval_flow(tmp_path: Path) -> None:
    app = create_app(tmp_path / "demo")
    client = TestClient(app)
    state = client.get("/api/state").json()
    assert len(state["drifts"]) == 7
    assert state["metrics"]["drift_rows_open"] == 7
    actionable = [item for item in state["drifts"] if item["can_writeback"]]
    assert len(actionable) == 2

    proposal_response = client.post("/api/proposals", json={"drift_ids": [actionable[0]["drift_id"]]})
    assert proposal_response.status_code == 201
    proposal = proposal_response.json()
    assert proposal["dry_run_shown"] is True
    assert "DRY RUN" in proposal["dry_run_text"]
    denied = client.post(
        f"/api/proposals/{proposal['change_set_id']}/approve",
        json={"actor_id": "analyst@example.invalid", "confirmed_dry_run": False},
    )
    assert denied.status_code == 400
    approved = client.post(
        f"/api/proposals/{proposal['change_set_id']}/approve",
        json={"actor_id": "analyst@example.invalid", "confirmed_dry_run": True},
    )
    assert approved.status_code == 200
    assert approved.json()["result"]["status"] == "APPLIED"
    assert approved.json()["metrics"]["drift_rows_open"] == 5
    replay = client.post(
        f"/api/proposals/{proposal['change_set_id']}/approve",
        json={"actor_id": "analyst@example.invalid", "confirmed_dry_run": True},
    )
    assert replay.status_code == 200
    assert replay.json()["result"]["status"] == "IDEMPOTENT_REPLAY"
    with connect_database(app.state.service.assets["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='writeback_applied'").fetchone()[0] == 1


def test_api_rejects_cross_source_batch_and_records_rejection(tmp_path: Path) -> None:
    app = create_app(tmp_path / "demo")
    client = TestClient(app)
    actionable = [item for item in client.get("/api/state").json()["drifts"] if item["can_writeback"]]
    response = client.post("/api/proposals", json={"drift_ids": [item["drift_id"] for item in actionable]})
    assert response.status_code == 409
    proposal = client.post("/api/proposals", json={"drift_ids": [actionable[0]["drift_id"]]}).json()
    rejected = client.post(
        f"/api/proposals/{proposal['change_set_id']}/reject",
        json={"actor_id": "reviewer@example.invalid", "reason": "Needs vendor confirmation"},
    )
    assert rejected.json()["status"] == "REJECTED"
    with connect_database(app.state.service.assets["database"]) as connection:
        row = connection.execute(
            "SELECT actor_id FROM audit_events WHERE event_type='proposal_rejected'"
        ).fetchone()
        assert row == ("reviewer@example.invalid",)


def test_api_serves_built_frontend_without_shadowing_api(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<h1>ShadowSync production UI</h1>", encoding="utf-8")
    app = create_app(tmp_path / "demo", frontend_dist=static)
    client = TestClient(app)
    assert client.get("/").text == "<h1>ShadowSync production UI</h1>"
    assert client.get("/api/health").json() == {"status": "ok", "data_classification": "synthetic"}
