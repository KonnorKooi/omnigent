import { authenticatedFetch } from "./identity";

// ── Wire types (snake_case from server) ─────────────────────────

interface DailyCostWire {
  day: string;
  cost_usd: number;
}

interface SessionUsageWire {
  id: string;
  created_at: number;
  updated_at: number;
  title: string | null;
  cost_usd: number;
  models: Record<string, number>;
  harness: string | null;
  other_harnesses: string[] | null;
  llm_model: string | null;
  agent_name: string | null;
}

interface UsageReportWire {
  cost_today: number;
  cost_last_7d: number;
  cost_last_30d: number;
  total_cost_usd: number;
  daily_costs: DailyCostWire[];
  sessions: SessionUsageWire[];
}

// ── App types (camelCase) ───────────────────────────────────────

export interface DailyCost {
  day: string;
  costUsd: number;
}

export interface SessionUsage {
  id: string;
  createdAt: number;
  updatedAt: number;
  title: string | null;
  costUsd: number;
  models: Record<string, number>;
  harness: string | null;
  otherHarnesses: string[] | null;
  llmModel: string | null;
  agentName: string | null;
}

export interface UsageReport {
  costToday: number;
  costLast7d: number;
  costLast30d: number;
  totalCostUsd: number;
  dailyCosts: DailyCost[];
  sessions: SessionUsage[];
}

// ── Fetch ───────────────────────────────────────────────────────

export async function fetchUsageReport(): Promise<UsageReport> {
  const res = await authenticatedFetch("/v1/usage");
  if (!res.ok) throw new Error(`Usage fetch failed: ${res.status}`);
  const wire: UsageReportWire = await res.json();
  return {
    costToday: wire.cost_today,
    costLast7d: wire.cost_last_7d,
    costLast30d: wire.cost_last_30d,
    totalCostUsd: wire.total_cost_usd,
    dailyCosts: (wire.daily_costs ?? []).map((d) => ({ day: d.day, costUsd: d.cost_usd })),
    sessions: (wire.sessions ?? []).map((s) => ({
      id: s.id,
      createdAt: s.created_at,
      updatedAt: s.updated_at,
      title: s.title,
      costUsd: s.cost_usd,
      models: s.models ?? {},
      harness: s.harness ?? null,
      otherHarnesses: s.other_harnesses ?? null,
      llmModel: s.llm_model ?? null,
      agentName: s.agent_name ?? null,
    })),
  };
}

// ── Subscription limits ─────────────────────────────────────────

interface UsageLimitWindowWire {
  id: string;
  label: string;
  used_percent: number | null;
  resets_at: number | null;
}

interface UsageLimitsReportWire {
  providers: { provider: string; windows: UsageLimitWindowWire[]; updated_at: number }[];
}

export interface UsageLimitWindow {
  id: string;
  label: string;
  /** Percent consumed (0–100); null when the window reset since the last reading. */
  usedPercent: number | null;
  /** Unix seconds when the window resets. */
  resetsAt: number | null;
}

export interface UsageLimitProvider {
  provider: string;
  windows: UsageLimitWindow[];
  updatedAt: number;
}

/** Latest subscription quota windows the server has seen from harness turns. */
export async function fetchUsageLimits(): Promise<UsageLimitProvider[]> {
  const res = await authenticatedFetch("/v1/usage/limits");
  if (!res.ok) throw new Error(`Usage limits fetch failed: ${res.status}`);
  const wire: UsageLimitsReportWire = await res.json();
  return (wire.providers ?? []).map((p) => ({
    provider: p.provider,
    updatedAt: p.updated_at,
    windows: (p.windows ?? []).map((w) => ({
      id: w.id,
      label: w.label,
      usedPercent: w.used_percent,
      resetsAt: w.resets_at,
    })),
  }));
}
