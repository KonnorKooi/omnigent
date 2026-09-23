import { useQuery } from "@tanstack/react-query";
import { fetchUsageLimits, type UsageLimitWindow } from "@/lib/usageApi";
import { cn } from "@/lib/utils";

const POLL_MS = 30_000;

const PROVIDER_NAMES: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  antigravity: "Antigravity",
};

/** "3:10 PM" within a day, else "Fri 3:10 PM". */
export function formatReset(resetsAt: number, now: number = Date.now()): string {
  const at = new Date(resetsAt * 1000);
  const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (resetsAt * 1000 - now < 24 * 3600 * 1000) return time;
  return `${at.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function meterTone(pct: number): string {
  if (pct >= 90) return "bg-destructive";
  if (pct >= 70) return "bg-amber-500";
  return "bg-foreground/50";
}

function LimitWindow({ window }: { window: UsageLimitWindow }) {
  const pct = window.usedPercent;
  const title =
    pct === null
      ? `${window.label}: reset — waiting for the next turn`
      : `${window.label}: ${Math.round(pct)}% used` +
        (window.resetsAt ? ` · resets ${formatReset(window.resetsAt)}` : "");
  return (
    <div className="flex min-w-0 items-center gap-1.5" title={title}>
      <span className="min-w-0 shrink truncate">{window.label}</span>
      <div className="h-1 min-w-6 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full", pct === null ? "" : meterTone(pct))}
          style={{ width: `${pct ?? 0}%` }}
        />
      </div>
      <span className="shrink-0 tabular-nums">{pct === null ? "–" : `${Math.round(pct)}%`}</span>
    </div>
  );
}

/**
 * Subscription usage limits (Claude / Codex / Antigravity) pinned to the
 * sidebar's bottom. Values arrive passively from harness turns, so a provider
 * appears only once a turn has run on it; renders nothing until then.
 */
export function SidebarUsageLimits() {
  const { data } = useQuery({
    queryKey: ["usage-limits"],
    queryFn: fetchUsageLimits,
    refetchInterval: POLL_MS,
    // A failing server (older build without the route) just hides the footer.
    retry: false,
  });
  if (!data || data.length === 0) return null;
  return (
    <div
      className="sidebar-compact-text shrink-0 space-y-1 border-t border-border/60 px-4 pt-2 pb-2 text-xs text-muted-foreground"
      data-testid="sidebar-usage-limits"
    >
      {data.map((p) => (
        <div key={p.provider} className="flex min-w-0 items-center gap-2">
          <span className="w-[4.5rem] shrink-0 truncate text-foreground/80">
            {PROVIDER_NAMES[p.provider] ?? p.provider}
          </span>
          <div
            className={cn(
              "grid min-w-0 flex-1 gap-x-2 gap-y-0.5",
              // Codex / Claude have two windows; Antigravity lists one per model.
              p.windows.length > 2 ? "grid-cols-1" : "auto-cols-fr grid-flow-col",
            )}
          >
            {p.windows.map((w) => (
              <LimitWindow key={w.id} window={w} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
