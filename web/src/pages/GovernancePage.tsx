/**
 * Admin governance table (``/settings/governance``). Rendered as a Settings
 * sub-category.
 *
 * Lists EVERY session on the server — live and imported, across all owners —
 * so an administrator can audit what agents did, for whom, and in which repo.
 * That is the one thing the sidebar's conversation list cannot answer: it is
 * scoped to the caller's own sessions.
 *
 * Gated on the client by `useIsAdmin()` AND on the server by the route itself
 * (403 for a non-admin). The client gate is UX only — it keeps a non-admin
 * from staring at a permanently failing table; the server is what enforces.
 *
 * Filter state lives in the URL, so a filtered view is a shareable link
 * ("everything in /repos/payments since Monday") rather than something an
 * admin has to re-derive by hand.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { PageScroll } from "@/components/PageScroll";
import { InfiniteScrollSentinel } from "@/components/InfiniteScrollSentinel";
import { SourceBadge } from "@/components/SourceBadge";
import { Input } from "@/components/ui/input";
import { useIsAdminStatus } from "@/hooks/useIsAdmin";
import { Link, useSearchParams } from "@/lib/routing";
import {
  GOVERNANCE_PAGE_SIZE,
  type GovernanceSession,
  type GovernanceSource,
  fetchGovernanceSessions,
} from "@/lib/governanceApi";

/**
 * Source values the filter offers, paired with their menu labels. Typed on the
 * closed {@link GovernanceSource} union — these are the choices this build
 * knows how to present, distinct from the open set the wire may carry.
 */
const SOURCE_OPTIONS: readonly { value: GovernanceSource; label: string }[] = [
  { value: "live", label: "Live" },
  { value: "import:claude", label: "Claude import" },
  { value: "import:codex", label: "Codex import" },
];

/**
 * Sentinel for the source select when the URL carries several `source`
 * values. The control can only express one, so rather than silently showing
 * the first (and destroying the rest on the next edit) it shows the real
 * multi-state. Picking a concrete option replaces the whole set, which is
 * what an explicit choice should do.
 */
const SOURCE_MULTI = "__multiple__";

/**
 * How long a free-text filter sits idle before it reaches the URL. Each
 * distinct filter value is its own react-query key against a list spanning
 * every session on the server, so committing per keystroke would fan a typed
 * path out into one server-wide query per character.
 */
const FILTER_DEBOUNCE_MS = 300;

/** Parse an epoch-seconds URL param, ignoring anything non-numeric. */
function epochParam(raw: string | null): number | undefined {
  // `Number("")` is 0, which would read as a real epoch bound — treat a blank
  // param the same as an absent one.
  if (raw === null || raw.trim() === "") return undefined;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function formatEpoch(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}

/**
 * Render a session's spend. `null` means the session was never priced, which
 * is a different answer from "it cost nothing" — showing `$0.00` there would
 * assert a zero the server never computed.
 */
function formatCost(totalCostUsd: number | null): string {
  if (totalCostUsd === null) return "—";
  return `$${totalCostUsd.toFixed(2)}`;
}

/** Placeholder for a column the session has no value for. */
function orDash(value: string | null): string {
  return value === null || value === "" ? "—" : value;
}

/**
 * A filter text box that keeps typing local and only publishes to the URL
 * once the user pauses. `value` remains the source of truth: when it changes
 * from outside (back/forward, a shared link, a reset) the draft re-syncs.
 *
 * A commit may CANONICALIZE what it writes — the owner box turns `"alice,"`
 * into `["alice"]`, which re-derives as `"alice"`. Re-syncing on that echo
 * would delete the comma the user just typed and make a second owner
 * unenterable, so `commit` reports the canonical form back and the resync is
 * skipped for that one echo. The guard is then disarmed, so a later
 * navigation back to the same value still wins. Any other change to `value`
 * came from outside and does re-sync.
 */
function DebouncedFilterInput({
  label,
  filterKey,
  value,
  placeholder,
  className,
  commit,
}: {
  label: string;
  filterKey: string;
  value: string;
  placeholder?: string;
  className?: string;
  /** Returns the canonical value written, when it differs from the input. */
  commit: (key: string, value: string) => string | void;
}) {
  const [draft, setDraft] = useState(value);
  const lastCommitted = useRef<string | null>(null);

  useEffect(() => {
    if (value === lastCommitted.current) {
      // Consume the echo. The guard is for one arrival — this box's own
      // canonicalized write coming back. Left armed, it would also swallow a
      // later external navigation back to that same value, and the debounce
      // below would then commit the stale draft over it.
      lastCommitted.current = null;
      return;
    }
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (draft === value) return;
    const timer = window.setTimeout(() => {
      lastCommitted.current = commit(filterKey, draft) ?? draft;
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draft, value, filterKey, commit]);

  return (
    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
      {label}
      <Input
        className={className}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
    </label>
  );
}

export function GovernancePage() {
  const { isAdmin, isPending: isAdminPending } = useIsAdminStatus();
  const [searchParams, setSearchParams] = useSearchParams();
  // Every filter is read from the URL, so a shared link reproduces the view.
  // `source` and `owner` are repeatable; the rest are single-valued. The date
  // bounds have no input control yet but are honoured when hand-set on a URL.
  const source = searchParams.getAll("source");
  const owner = searchParams.getAll("owner");
  const workspacePrefix = searchParams.get("workspace") ?? "";
  const updatedAfter = epochParam(searchParams.get("updated_after"));
  const updatedBefore = epochParam(searchParams.get("updated_before"));
  const searchQuery = searchParams.get("q") ?? "";

  /**
   * Write a filter into the URL, replacing every value it currently holds.
   * `replace` keeps filtering from stacking history entries. Changing a
   * filter drops the paging cursor implicitly — the query key changes, so
   * react-query starts a fresh page sequence.
   */
  const setFilterValues = useCallback(
    (key: string, values: string[]) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete(key);
          for (const value of values) if (value !== "") next.append(key, value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  /** Single-valued filter write (search, workspace, source). */
  const setFilter = useCallback(
    (key: string, value: string) => setFilterValues(key, [value]),
    [setFilterValues],
  );

  /**
   * Owner is repeatable on the wire, so the box reads and writes the whole
   * list as a comma-separated string. Rendering only the first value would
   * hide the rest and silently drop them on the next edit.
   *
   * Returns the canonical string the URL will re-derive to, so the input can
   * tell its own echo apart from an outside change and not eat a trailing
   * comma mid-entry.
   */
  const setOwnerFilter = useCallback(
    (_key: string, value: string): string => {
      const owners = value
        .split(",")
        .map((entry) => entry.trim())
        .filter((entry) => entry !== "");
      setFilterValues("owner", owners);
      return owners.join(", ");
    },
    [setFilterValues],
  );

  const { data, isPending, isError, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useInfiniteQuery({
      // Positional key, fixed argument order — mirrors `useConversations`.
      queryKey: [
        "governance-sessions",
        source,
        owner,
        workspacePrefix,
        updatedAfter,
        updatedBefore,
        searchQuery,
      ],
      queryFn: ({ pageParam }) =>
        fetchGovernanceSessions({
          after: pageParam as string | undefined,
          limit: GOVERNANCE_PAGE_SIZE,
          source,
          owner,
          workspacePrefix,
          updatedAfter,
          updatedBefore,
          searchQuery,
        }),
      initialPageParam: undefined as string | undefined,
      getNextPageParam: (last) => (last.hasMore ? (last.lastId ?? undefined) : undefined),
      // A non-admin gets a 403 from the server; don't fire the request at all.
      enabled: isAdmin,
      staleTime: 30_000,
      // Every filter change is a new cache key with no data of its own. Without
      // this the table unmounts and the page blanks to "Loading…" on each
      // change; holding the previous rows keeps the surface stable while the
      // new page arrives.
      placeholderData: keepPreviousData,
    });

  // Until `/v1/me` answers, `isAdmin` is a seeded `false` that means "not
  // asked yet" as often as "not an admin". Rendering the denial now would
  // accuse a real admin who deep-linked here on a cold load.
  if (isAdminPending) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <PageScroll contentClassName="px-8" extraBottom="2.5rem">
        <h1 className="mb-2 text-2xl font-semibold">Governance</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to view governance data.
        </p>
      </PageScroll>
    );
  }

  const sessions: GovernanceSession[] = (data?.pages ?? []).flatMap((p) => p.data);

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem" maxWidthClassName="max-w-7xl">
      <div className="mb-1 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Governance</h1>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Every session on this server, including sessions owned by other members and transcripts
        imported from other agents.
      </p>

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <DebouncedFilterInput
          label="Search"
          filterKey="q"
          className="w-56"
          placeholder="Title or content"
          value={searchQuery}
          commit={setFilter}
        />
        <DebouncedFilterInput
          label="Workspace"
          filterKey="workspace"
          className="w-64"
          placeholder="/repos/…"
          value={workspacePrefix}
          commit={setFilter}
        />
        <DebouncedFilterInput
          label="Owner"
          filterKey="owner"
          className="w-40"
          placeholder="username, …"
          value={owner.join(", ")}
          commit={setOwnerFilter}
        />
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Source
          <select
            className="h-9 rounded-md border border-border bg-transparent px-2 text-sm text-foreground"
            value={source.length > 1 ? SOURCE_MULTI : (source[0] ?? "")}
            onChange={(e) => {
              // The multi-state entry exists to display a URL the control
              // cannot express; re-picking it would mean nothing.
              if (e.target.value === SOURCE_MULTI) return;
              setFilter("source", e.target.value);
            }}
          >
            <option value="">All sources</option>
            {source.length > 1 && <option value={SOURCE_MULTI}>Multiple ({source.length})</option>}
            {SOURCE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {isError && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Could not load governance data. {error instanceof Error ? error.message : ""}
        </div>
      )}

      {isPending && !isError && <p className="text-sm text-muted-foreground">Loading…</p>}

      {/* `hasNextPage` guards the auto-paging window: a first page consumed
          entirely by rows the filter excludes leaves `sessions` empty while
          the sentinel is already fetching the next one. "No sessions match"
          is the one message an audit surface must not show falsely. */}
      {!isPending && !isError && sessions.length === 0 && !hasNextPage && (
        <p className="text-sm text-muted-foreground">No sessions match these filters.</p>
      )}

      {sessions.length > 0 && (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Title</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 font-medium">Owner</th>
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Workspace</th>
                <th className="px-3 py-2 font-medium">Branch</th>
                <th className="px-3 py-2 font-medium">Updated</th>
                <th className="px-3 py-2 text-right font-medium">Items</th>
                <th className="px-3 py-2 text-right font-medium">Cost</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id} className="border-t border-border">
                  <td className="px-3 py-2 align-middle">
                    {/* The link lives on the title rather than the whole row:
                        a <tr> can't be wrapped in an anchor, and a row-level
                        onClick would not be keyboard reachable. The hover
                        affordance stays on the link for the same reason —
                        highlighting the whole row would advertise a click
                        target that isn't there. */}
                    <Link
                      to={`/settings/governance/${encodeURIComponent(s.id)}`}
                      className="font-medium hover:underline"
                    >
                      {s.title ?? "Untitled session"}
                    </Link>
                  </td>
                  <td className="px-3 py-2 align-middle">
                    <SourceBadge source={s.source} />
                  </td>
                  <td className="px-3 py-2 align-middle">{orDash(s.owner)}</td>
                  <td className="px-3 py-2 align-middle text-muted-foreground">
                    {orDash(s.agentId)}
                  </td>
                  <td className="px-3 py-2 align-middle font-mono text-xs">
                    {orDash(s.workspace)}
                  </td>
                  <td className="px-3 py-2 align-middle text-muted-foreground">
                    {orDash(s.gitBranch)}
                  </td>
                  <td className="px-3 py-2 align-middle text-muted-foreground">
                    {formatEpoch(s.updatedAt)}
                  </td>
                  <td className="px-3 py-2 text-right align-middle tabular-nums">{s.itemCount}</td>
                  <td className="px-3 py-2 text-right align-middle tabular-nums">
                    {formatCost(s.totalCostUsd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* No scrollRoot: this table scrolls with the page, so the viewport is
          the right intersection root. */}
      <InfiniteScrollSentinel
        className="mt-3"
        hasMore={hasNextPage}
        isFetching={isFetchingNextPage}
        fetchMore={fetchNextPage}
      />
    </PageScroll>
  );
}
