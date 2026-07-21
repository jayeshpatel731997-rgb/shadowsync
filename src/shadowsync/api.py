"""FastAPI orchestration layer for the governed ShadowSync workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from shadowsync.drift import classify_drift, detect_drift
from shadowsync.governance import (
    ApprovalRequiredError,
    ConcurrencyConflictError,
    GovernanceError,
    InvalidChangeSetError,
    apply_writeback,
    emit_audit,
    observe_outcome,
    propose_writeback,
    request_approval,
    resolve_ambiguous_drift,
)
from shadowsync.ingest import ingest_bi_extract, ingest_excel, read_all_sql_sor, read_sql_sor
from shadowsync.models import AuthoritativeSide, ChangeSet, Drift, Resolution, SourceSystem
from shadowsync.seed import ensure_database_schema, generate_demo


class ProposalRequest(BaseModel):
    drift_ids: list[str] = Field(min_length=1, max_length=25)


class ApprovalRequest(BaseModel):
    actor_id: str = Field(min_length=2, max_length=120)
    confirmed_dry_run: bool


class RejectionRequest(BaseModel):
    actor_id: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=3, max_length=500)


@dataclass(slots=True)
class StoredProposal:
    change_set: ChangeSet
    status: str = "PENDING"


class ShadowSyncService:
    """One bounded orchestrator over the typed deterministic tools."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.assets = self._ensure_assets()
        self.proposals: dict[str, StoredProposal] = {}
        self.lock = Lock()
        initial = self.metrics()
        self.trend = [
            {"label": "Seeded", "drift_rows_open": initial["drift_rows_open"]},
            {"label": "Current", "drift_rows_open": initial["drift_rows_open"]},
        ]

    def _ensure_assets(self) -> dict[str, Path]:
        expected = {
            "database": self.data_dir / "shadowsync.db",
            "excel": self.data_dir / "analyst_shadow.xlsx",
            "bi_extract": self.data_dir / "power_bi_extract.csv",
            "manual_baseline": self.data_dir / "manual_baseline.csv",
        }
        if not all(path.is_file() for path in expected.values()):
            return generate_demo(self.data_dir)
        ensure_database_schema(expected["database"])
        return expected

    def _all_drift(self) -> tuple[list[Drift], dict[str, Resolution | None]]:
        excel_rows = ingest_excel(self.assets["excel"])
        bi_rows = ingest_bi_extract(self.assets["bi_extract"])
        sor_all = read_all_sql_sor(self.assets["database"])
        bi_tables = sorted({row.table for row in bi_rows})
        bi_sor = [row for table in bi_tables for row in read_sql_sor(self.assets["database"], table)]
        drift = detect_drift(excel_rows, sor_all) + detect_drift(bi_rows, bi_sor)
        rows_by_source = {
            SourceSystem.EXCEL: {(row.table, row.key): row for row in excel_rows},
            SourceSystem.POWER_BI: {(row.table, row.key): row for row in bi_rows},
        }
        resolutions: dict[str, Resolution | None] = {}
        for item in drift:
            classification = classify_drift(item)
            if classification.authoritative_side is not AuthoritativeSide.AMBIGUOUS:
                resolutions[item.drift_id] = None
                continue
            peer_source = SourceSystem.POWER_BI if item.source is SourceSystem.EXCEL else SourceSystem.EXCEL
            peer = rows_by_source[peer_source].get((item.table, item.key))
            peer_values = [] if peer is None or item.field not in peer.values else [peer.values[item.field]]
            resolutions[item.drift_id] = resolve_ambiguous_drift(item, {"peer_source_values": peer_values})
        return sorted(drift, key=lambda item: item.drift_id), resolutions

    def drift_queue(self) -> list[dict[str, Any]]:
        drift, resolutions = self._all_drift()
        result = []
        for item in drift:
            classification = classify_drift(item)
            resolution = resolutions[item.drift_id]
            recommended_side = resolution.authoritative_side if resolution else classification.authoritative_side
            result.append(
                {
                    **asdict(item),
                    "drift_id": item.drift_id,
                    "source": item.source.value,
                    "drift_type": item.drift_type.value,
                    "authoritative_side": classification.authoritative_side.value,
                    "confidence": classification.confidence,
                    "rule": classification.rule,
                    "recommended_side": recommended_side.value,
                    "rationale": resolution.rationale if resolution else classification.rule,
                    "can_writeback": recommended_side is AuthoritativeSide.SOURCE and item.field != "__row__",
                }
            )
        return result

    def metrics(self) -> dict[str, Any]:
        return asdict(
            observe_outcome(
                self.assets["database"], self.assets["excel"], self.assets["bi_extract"], self.assets["manual_baseline"]
            )
        )

    def create_proposal(self, drift_ids: list[str]) -> ChangeSet:
        if len(set(drift_ids)) != len(drift_ids):
            raise InvalidChangeSetError("duplicate drift IDs are not allowed")
        drift, resolutions = self._all_drift()
        index = {item.drift_id: item for item in drift}
        if any(identifier not in index for identifier in drift_ids):
            raise InvalidChangeSetError("one or more drift IDs are stale or unknown")
        selected = [index[identifier] for identifier in drift_ids]
        selected_resolutions = {
            item.drift_id: resolutions[item.drift_id]
            for item in selected
            if resolutions[item.drift_id] is not None
        }
        change_set = propose_writeback(selected, selected_resolutions)
        self.proposals[change_set.change_set_id] = StoredProposal(change_set)
        return change_set

    def approve(self, change_set_id: str, actor_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise GovernanceError("the exact dry-run must be confirmed")
        with self.lock:
            stored = self.proposals.get(change_set_id)
            if stored is None:
                raise InvalidChangeSetError("proposal is unknown or expired")
            gate = request_approval(
                self.assets["database"], stored.change_set, actor_id, max_changes=len(stored.change_set.changes)
            )
            result = apply_writeback(self.assets["database"], stored.change_set, gate.approval_token)
            stored.status = result.status
            metrics = self.metrics()
            if result.status == "APPLIED":
                self.trend.append(
                    {"label": f"Batch {len(self.trend) - 1}", "drift_rows_open": metrics["drift_rows_open"]}
                )
            return {"result": asdict(result), "metrics": metrics, "trend": self.trend}

    def reject(self, change_set_id: str, actor_id: str, reason: str) -> dict[str, str]:
        with self.lock:
            stored = self.proposals.get(change_set_id)
            if stored is None:
                raise InvalidChangeSetError("proposal is unknown or expired")
            if stored.status != "PENDING":
                raise GovernanceError("proposal is no longer pending")
            emit_audit(
                self.assets["database"], "proposal_rejected", actor_id,
                {"change_set_id": change_set_id, "reason": reason},
            )
            stored.status = "REJECTED"
            return {"change_set_id": change_set_id, "status": stored.status}

    def state(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "drifts": self.drift_queue(),
            "metrics": self.metrics(),
            "trend": self.trend,
            "proposals": [
                {
                    "change_set_id": stored.change_set.change_set_id,
                    "status": stored.status,
                    "dry_run_text": stored.change_set.dry_run_text,
                    "change_count": len(stored.change_set.changes),
                }
                for stored in self.proposals.values()
            ],
        }


def create_app(data_dir: Path | None = None, frontend_dist: Path | None = None) -> FastAPI:
    service = ShadowSyncService(data_dir or Path(os.getenv("SHADOWSYNC_DATA_DIR", "demo")))
    app = FastAPI(title="ShadowSync API", version="0.1.0")
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "data_classification": "synthetic"}

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return service.state()

    @app.post("/api/proposals", status_code=201)
    def create_proposal(body: ProposalRequest) -> dict[str, Any]:
        try:
            return asdict(service.create_proposal(body.drift_ids))
        except (GovernanceError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/proposals/{change_set_id}/approve")
    def approve(change_set_id: str, body: ApprovalRequest) -> dict[str, Any]:
        try:
            return service.approve(change_set_id, body.actor_id, body.confirmed_dry_run)
        except ConcurrencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ApprovalRequiredError, GovernanceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/proposals/{change_set_id}/reject")
    def reject(change_set_id: str, body: RejectionRequest) -> dict[str, str]:
        try:
            return service.reject(change_set_id, body.actor_id, body.reason)
        except (GovernanceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    static_dir = frontend_dist or Path(os.getenv("SHADOWSYNC_FRONTEND_DIST", "frontend/dist"))
    if (static_dir / "index.html").is_file():
        # Mounted last so every governed /api route retains precedence.
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    return app


app = create_app()
