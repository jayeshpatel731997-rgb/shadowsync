"""Run the fixed ShadowSync demo with an explicit attributable approval flag."""

from __future__ import annotations

import argparse
from pathlib import Path

from shadowsync.drift import detect_drift
from shadowsync.governance import (
    apply_writeback,
    observe_outcome,
    propose_writeback,
    request_approval,
    resolve_ambiguous_drift,
)
from shadowsync.ingest import ingest_bi_extract, ingest_excel, read_all_sql_sor
from shadowsync.models import AuthoritativeSide
from shadowsync.seed import generate_demo


def run_demo(output_dir: Path, approve_as: str | None = None) -> int:
    """Generate assets, print the dry-run, and apply only with explicit identity."""
    assets = generate_demo(output_dir)
    before = observe_outcome(
        assets["database"], assets["excel"], assets["bi_extract"], assets["manual_baseline"]
    )
    excel_drift = detect_drift(ingest_excel(assets["excel"]), read_all_sql_sor(assets["database"]))
    peer_rows = {(row.table, row.key): row for row in ingest_bi_extract(assets["bi_extract"])}
    resolutions = {}
    for item in excel_drift:
        try:
            peer = peer_rows.get((item.table, item.key))
            peer_values = [] if peer is None or item.field not in peer.values else [peer.values[item.field]]
            resolution = resolve_ambiguous_drift(item, {"peer_source_values": peer_values})
        except Exception:
            continue
        if resolution.authoritative_side is AuthoritativeSide.SOURCE:
            resolutions[item.drift_id] = resolution

    change_set = propose_writeback(excel_drift, resolutions)
    print(f"Before: {before.drift_rows_open} drift rows / {before.drift_fields_open} fields open")
    print(change_set.dry_run_text)
    if approve_as is None:
        print("No write performed. Re-run with --approve-as ID after reviewing the dry-run.")
        return 0

    gate = request_approval(
        assets["database"], change_set, approve_as, max_changes=len(change_set.changes)
    )
    result = apply_writeback(assets["database"], change_set, gate.approval_token)
    after = observe_outcome(
        assets["database"], assets["excel"], assets["bi_extract"], assets["manual_baseline"]
    )
    replay = apply_writeback(assets["database"], change_set, gate.approval_token)
    print(f"Approval: {result.actor_id} · {result.status} · {result.applied_count} field(s)")
    print(f"Replay: {replay.status} · {replay.applied_count} additional writes")
    print(f"After: {after.drift_rows_open} drift rows / {after.drift_fields_open} fields open")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("demo"))
    parser.add_argument(
        "--approve-as",
        help="Explicit synthetic approver identity. Omit to show the dry-run without writing.",
    )
    args = parser.parse_args()
    raise SystemExit(run_demo(args.output_dir, args.approve_as))


if __name__ == "__main__":
    main()
