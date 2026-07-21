export type Drift = {
  drift_id: string;
  source: "excel" | "power_bi";
  table: string;
  key: string;
  field: string;
  source_value: unknown;
  sor_value: unknown;
  drift_type: string;
  authoritative_side: "source" | "sor" | "ambiguous";
  recommended_side: "source" | "sor" | "ambiguous";
  confidence: number;
  rationale: string;
  can_writeback: boolean;
};

export type Metrics = {
  drift_rows_open: number;
  drift_fields_open: number;
  manual_baseline_minutes: number;
  governed_reconciliation_minutes: number | null;
  time_improvement_percent: number | null;
};

export type TrendPoint = { label: string; drift_rows_open: number };

export type AppState = {
  generated_at: string;
  drifts: Drift[];
  metrics: Metrics;
  trend: TrendPoint[];
  proposals: Array<{ change_set_id: string; status: string; dry_run_text: string; change_count: number }>;
};

export type ChangeSet = {
  change_set_id: string;
  source: string;
  created_at: string;
  dry_run_text: string;
  dry_run_shown: boolean;
  changes: Array<{
    change_id: string;
    table: string;
    key: string;
    field: string;
    old_value: unknown;
    new_value: unknown;
    expected_row_version: number;
  }>;
};
