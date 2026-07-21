import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const state = {
  generated_at: "2026-07-21T15:00:00Z",
  metrics: { drift_rows_open: 7, drift_fields_open: 7, manual_baseline_minutes: 82, governed_reconciliation_minutes: null, time_improvement_percent: null },
  trend: [{ label: "Seeded", drift_rows_open: 7 }, { label: "Current", drift_rows_open: 7 }],
  proposals: [],
  drifts: [{
    drift_id: "excel:open_orders:PO-1001:unit_price_cents", source: "excel", table: "open_orders", key: "PO-1001", field: "unit_price_cents",
    source_value: 1195, sor_value: 1250, drift_type: "stale_price", authoritative_side: "ambiguous", recommended_side: "source",
    confidence: 0.5, rationale: "corroborated by Power BI", can_writeback: true,
  }, {
    drift_id: "excel:allocations:AL-999:__row__", source: "excel", table: "allocations", key: "AL-999", field: "__row__",
    source_value: {}, sor_value: null, drift_type: "orphaned_allocation", authoritative_side: "sor", recommended_side: "sor",
    confidence: 1, rationale: "sql_system_of_record", can_writeback: false,
  }],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ShadowSync workspace", () => {
  it("renders drift evidence and only enables reviewable rows", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => state }));
    render(<App />);
    expect(await screen.findByText("PO-1001")).toBeInTheDocument();
    expect(screen.getByText("Drift requiring review")).toBeInTheDocument();
    expect(screen.getByLabelText("Select Excel AL-999 __row__")).toBeDisabled();
    expect(screen.getByRole("button", { name: /prepare dry-run/i })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Select Excel PO-1001 unit_price_cents"));
    expect(screen.getByRole("button", { name: /prepare dry-run/i })).toBeEnabled();
  });

  it("shows the immutable dry-run before approval controls", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => state })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ change_set_id: "cs-1", source: "excel", created_at: state.generated_at, dry_run_shown: true, dry_run_text: "DRY RUN — 1 SQL field update", changes: [{ change_id: "c-1", table: "open_orders", key: "PO-1001", field: "unit_price_cents", old_value: 1250, new_value: 1195, expected_row_version: 1 }] }) });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    fireEvent.click(await screen.findByLabelText("Select Excel PO-1001 unit_price_cents"));
    fireEvent.click(screen.getByRole("button", { name: /prepare dry-run/i }));
    expect(await screen.findByRole("heading", { name: "Review exact SQL diff" })).toBeInTheDocument();
    expect(screen.getByText("No write has occurred.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve & apply/i })).toBeDisabled();
    fireEvent.click(screen.getByText(/I reviewed this exact dry-run/));
    await waitFor(() => expect(screen.getByRole("button", { name: /approve & apply/i })).toBeEnabled());
  });
});
