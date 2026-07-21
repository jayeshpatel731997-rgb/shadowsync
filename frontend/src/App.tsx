import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { AppState, ChangeSet, Drift, TrendPoint } from "./types";

const labels: Record<string, string> = {
  stale_price: "Stale price",
  orphaned_allocation: "Orphaned allocation",
  quantity_mismatch: "Quantity mismatch",
  uom_inconsistency: "UOM inconsistency",
  missing_attribute: "Missing attribute",
  field_mismatch: "Field mismatch",
};

const value = (input: unknown) => {
  if (input === null || input === "") return "Blank";
  if (typeof input === "object") {
    const record = input as Record<string, unknown>;
    return String(record.allocation_id ?? record.order_id ?? record.sku ?? "Structured row");
  }
  return String(input);
};
const sourceLabel = (source: Drift["source"]) => source === "power_bi" ? "Power BI" : "Excel";

function Trend({ points }: { points: TrendPoint[] }) {
  const max = Math.max(...points.map((point) => point.drift_rows_open), 1);
  return (
    <div className="trend" role="img" aria-label={`Drift-open trend: ${points.map((p) => `${p.label} ${p.drift_rows_open}`).join(", ")}`}>
      {points.map((point, index) => (
        <div className="trend-point" key={`${point.label}-${index}`}>
          <span className="trend-value">{point.drift_rows_open}</span>
          <div className="trend-track"><span style={{ height: `${Math.max((point.drift_rows_open / max) * 100, 8)}%` }} /></div>
          <span className="trend-label">{point.label}</span>
        </div>
      ))}
    </div>
  );
}

function DriftRow({ drift, selected, disabled, onToggle }: {
  drift: Drift; selected: boolean; disabled: boolean; onToggle: () => void;
}) {
  return (
    <tr className={selected ? "selected-row" : ""}>
      <td className="select-cell">
        <input
          aria-label={`Select ${sourceLabel(drift.source)} ${drift.key} ${drift.field}`}
          type="checkbox"
          checked={selected}
          disabled={!drift.can_writeback || disabled}
          onChange={onToggle}
          title={!drift.can_writeback ? "SQL remains authoritative; no write-back proposed" : undefined}
        />
      </td>
      <td><span className={`source source-${drift.source}`}>{sourceLabel(drift.source)}</span></td>
      <td><strong>{drift.key}</strong><small>{drift.table}</small></td>
      <td><span className={`type type-${drift.drift_type}`}>{labels[drift.drift_type] ?? drift.drift_type}</span></td>
      <td><strong>{drift.field}</strong><small>{drift.rationale}</small></td>
      <td className="value-cell source-value">{value(drift.source_value)}</td>
      <td className="value-cell">{value(drift.sor_value)}</td>
      <td><span className={`authority authority-${drift.recommended_side}`}>{drift.recommended_side === "sor" ? "Keep SQL" : "Review write-back"}</span></td>
    </tr>
  );
}

export function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sourceFilter, setSourceFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [proposal, setProposal] = useState<ChangeSet | null>(null);
  const [actor, setActor] = useState("analyst@example.invalid");
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState("Needs additional operational evidence");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = async () => {
    try {
      setError("");
      setState(await api.state());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load ShadowSync");
    }
  };

  useEffect(() => { void load(); }, []);

  const selectedSource = state?.drifts.find((item) => selected.has(item.drift_id))?.source;
  const filtered = useMemo(() => state?.drifts.filter((item) =>
    (sourceFilter === "all" || item.source === sourceFilter) &&
    (typeFilter === "all" || item.drift_type === typeFilter)
  ) ?? [], [state, sourceFilter, typeFilter]);

  const toggle = (drift: Drift) => {
    setSelected((current) => {
      const next = new Set(current);
      next.has(drift.drift_id) ? next.delete(drift.drift_id) : next.add(drift.drift_id);
      return next;
    });
  };

  const createProposal = async () => {
    setBusy(true); setError(""); setMessage("");
    try { setProposal(await api.propose([...selected])); setConfirmed(false); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Proposal failed"); }
    finally { setBusy(false); }
  };

  const approve = async () => {
    if (!proposal) return;
    setBusy(true); setError("");
    try {
      const result = await api.approve(proposal.change_set_id, actor, confirmed);
      setProposal(null); setSelected(new Set()); setConfirmed(false);
      await load();
      setMessage(`${result.result.applied_count} governed change applied. Drift evidence refreshed.`);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Approval failed"); }
    finally { setBusy(false); }
  };

  const reject = async () => {
    if (!proposal) return;
    setBusy(true); setError("");
    try {
      await api.reject(proposal.change_set_id, actor, reason);
      setProposal(null); setSelected(new Set()); setConfirmed(false);
      await load();
      setMessage("Proposal rejected and recorded in the append-only audit.");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Rejection failed"); }
    finally { setBusy(false); }
  };

  if (!state && !error) return <main className="center-state"><span className="spinner" />Loading governed workspace…</main>;
  if (!state) return <main className="center-state error-state"><strong>Workspace unavailable</strong><span>{error}</span><button onClick={load}>Try again</button></main>;

  const metrics = state.metrics;
  const types = [...new Set(state.drifts.map((item) => item.drift_type))];
  const governedCycle = metrics.governed_reconciliation_minutes === null
    ? "—"
    : metrics.governed_reconciliation_minutes < 1 ? "<1 min" : `${metrics.governed_reconciliation_minutes.toFixed(0)} min`;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">SS</span><div><strong>ShadowSync</strong><span>Governed reconciliation</span></div></div>
        <div className="environment"><span className="status-dot" />Synthetic workspace <kbd>v0.1</kbd></div>
      </header>

      <div className="prototype-banner" role="note">
        <strong>Synthetic, non-commercial portfolio prototype.</strong> Learning/demo only · no customers · no proprietary data
      </div>

      <main className="workspace">
        <section className="hero">
          <div><span className="eyebrow">Operations control room</span><h1>Reconcile what changed.<br /><em>Govern what writes.</em></h1></div>
          <div className="sync-status"><span>Last evidence scan</span><strong>{new Date(state.generated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</strong><small>Excel · Power BI · SQLite</small></div>
        </section>

        <section className="kpi-grid" aria-label="Reconciliation KPIs">
          <article className="kpi-card primary"><span>Drift rows open</span><strong>{metrics.drift_rows_open}</strong><small>{metrics.drift_fields_open} field-level differences</small></article>
          <article className="kpi-card"><span>Manual baseline</span><strong>{metrics.manual_baseline_minutes.toFixed(0)}<small> min</small></strong><small>Average historical resolution</small></article>
          <article className="kpi-card"><span>Governed cycle</span><strong>{governedCycle}</strong><small>{metrics.time_improvement_percent === null ? "Awaiting approved batch" : `${metrics.time_improvement_percent.toFixed(0)}% faster than baseline`}</small></article>
          <article className="kpi-card trend-card"><div><span>Drift-open trend</span><small>Lower is better</small></div><Trend points={state.trend} /></article>
        </section>

        {(message || error) && <div className={error ? "notice notice-error" : "notice notice-success"} role="status"><span>{error || message}</span><button aria-label="Dismiss message" onClick={() => { setError(""); setMessage(""); }}>×</button></div>}

        <section className="queue-card">
          <div className="queue-header">
            <div><span className="eyebrow">Live exception queue</span><h2>Drift requiring review</h2><p>Only corroborated analyst values can enter a SQL write-back proposal.</p></div>
            <div className="filters">
              <label>Source<select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option value="all">All sources</option><option value="excel">Excel</option><option value="power_bi">Power BI</option></select></label>
              <label>Drift type<select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">All types</option>{types.map((type) => <option value={type} key={type}>{labels[type] ?? type}</option>)}</select></label>
            </div>
          </div>

          <div className="table-wrap">
            <table>
              <thead><tr><th><span className="sr-only">Select</span></th><th>Source</th><th>Record</th><th>Drift type</th><th>Field / evidence</th><th>Analyst value</th><th>SQL value</th><th>Recommendation</th></tr></thead>
              <tbody>{filtered.length ? filtered.map((drift) => <DriftRow key={drift.drift_id} drift={drift} selected={selected.has(drift.drift_id)} disabled={Boolean(selectedSource && selectedSource !== drift.source)} onToggle={() => toggle(drift)} />) : <tr><td colSpan={8} className="empty">No drift matches these filters.</td></tr>}</tbody>
            </table>
          </div>
          <div className="queue-footer"><span>{selected.size ? `${selected.size} selected · bounded to ${selectedSource === "power_bi" ? "Power BI" : "Excel"}` : "Select a reviewable drift to prepare an immutable dry-run"}</span><button className="button primary-button" disabled={!selected.size || busy} onClick={createProposal}>{busy ? "Working…" : "Prepare dry-run"}<span aria-hidden>→</span></button></div>
        </section>
      </main>

      {proposal && <div className="drawer-backdrop" role="presentation">
        <aside className="drawer" aria-labelledby="dry-run-title">
          <div className="drawer-head"><div><span className="eyebrow">Approval gate</span><h2 id="dry-run-title">Review exact SQL diff</h2></div><button className="icon-button" aria-label="Close dry-run" onClick={() => setProposal(null)}>×</button></div>
          <div className="gate-callout"><span>G1</span><p><strong>No write has occurred.</strong><br />Approval is bound to this exact change-set hash and batch size.</p></div>
          <div className="diff-list">{proposal.changes.map((change) => <article className="diff-item" key={change.change_id}><div><strong>{change.key}</strong><span>{change.table} · {change.field}</span></div><div className="diff-values"><span><small>SQL now</small>{value(change.old_value)}</span><b>→</b><span><small>Proposed</small>{value(change.new_value)}</span></div><code>row_version = {change.expected_row_version}</code></article>)}</div>
          <details><summary>Raw dry-run evidence</summary><pre>{proposal.dry_run_text}</pre></details>
          <label className="field-label">Approver identity<input value={actor} onChange={(event) => setActor(event.target.value)} required /></label>
          <label className="confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>I reviewed this exact dry-run and authorize this bounded batch.</span></label>
          <div className="reject-block"><label className="field-label">Rejection reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label></div>
          <div className="drawer-actions"><button className="button secondary-button" disabled={busy || reason.trim().length < 3} onClick={reject}>Reject & audit</button><button className="button primary-button" disabled={busy || !confirmed || actor.trim().length < 2} onClick={approve}>{busy ? "Applying…" : "Approve & apply"}</button></div>
          <p className="governance-note">Optimistic concurrency will reject this batch if SQL changed after the diff was computed.</p>
        </aside>
      </div>}

      <footer><strong>Data reconciliation only.</strong> Not a money-recovery, deduction, chargeback, or claims agent.</footer>
    </div>
  );
}
