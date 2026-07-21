import type { AppState, ChangeSet } from "./types";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: "Unexpected server response" }));
    throw new Error(payload.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  state: () => request<AppState>("/api/state"),
  propose: (driftIds: string[]) =>
    request<ChangeSet>("/api/proposals", { method: "POST", body: JSON.stringify({ drift_ids: driftIds }) }),
  approve: (changeSetId: string, actorId: string, confirmedDryRun: boolean) =>
    request<{ result: { status: string; applied_count: number }; metrics: AppState["metrics"]; trend: AppState["trend"] }>(
      `/api/proposals/${changeSetId}/approve`,
      { method: "POST", body: JSON.stringify({ actor_id: actorId, confirmed_dry_run: confirmedDryRun }) },
    ),
  reject: (changeSetId: string, actorId: string, reason: string) =>
    request<{ status: string }>(`/api/proposals/${changeSetId}/reject`, {
      method: "POST",
      body: JSON.stringify({ actor_id: actorId, reason }),
    }),
};
