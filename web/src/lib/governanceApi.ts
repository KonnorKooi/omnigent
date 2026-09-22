// Typed client for the admin-only `/v1/governance` routes, which list EVERY
// session on the server (not just the caller's) so an administrator can audit
// what agents did, for whom, and in which repo. Mirrors
// `omnigent/server/routes/governance.py`.
//
// Naming follows `sessionsApi.ts`: the TS surface is camelCase, the wire is
// snake_case, and conversion happens at the parse boundary so callers never
// see raw wire fields. Non-2xx throws an `ApiError` (react-query needs a throw
// to populate `isError`) — the 403 a non-admin gets surfaces that way.

import { ApiError } from "./sessionsApi";
import { authenticatedFetch } from "./identity";

/**
 * Where a session came from. `live` was created and run on this server;
 * `import:*` was ingested from another agent's on-disk transcript, so its
 * items are read-only history rather than a resumable conversation.
 *
 * These are the values the filter UI OFFERS, not the closed set the wire can
 * carry: the server derives its own set from `typing.get_args(ImportSource)`,
 * so adding an importer widens the wire without touching this type. Wire
 * fields stay `string` for that reason — see {@link GovernanceSessionWire}.
 */
export type GovernanceSource = "live" | "import:claude" | "import:codex";

/** Wire shape of one governance list row (snake_case). */
interface GovernanceSessionWire {
  id: string;
  title?: string | null;
  /** Open — a new server-side import source is a value this client hasn't heard of. */
  source: string;
  external_session_id?: string | null;
  owner?: string | null;
  agent_id?: string | null;
  workspace?: string | null;
  git_branch?: string | null;
  item_count: number;
  /** `null` = never priced. Distinct from a genuine zero; renders as "—". */
  total_cost_usd?: number | null;
  created_at: number;
  updated_at: number;
}

interface GovernanceSessionsPageWire {
  data: GovernanceSessionWire[];
  first_id: string | null;
  last_id: string | null;
  has_more: boolean;
}

/** One session in the governance list, as the UI consumes it. */
export interface GovernanceSession {
  id: string;
  title: string | null;
  /**
   * Open rather than a closed union — the server derives its source set from
   * `ImportSource`, so this client can legitimately receive a value it has
   * never heard of. `SourceBadge` renders an unrecognized value verbatim.
   */
  source: string;
  externalSessionId: string | null;
  owner: string | null;
  agentId: string | null;
  workspace: string | null;
  gitBranch: string | null;
  itemCount: number;
  /**
   * Total spend attributed to the session, or `null` when it was never
   * priced. `null` and `0` are different answers — render `null` as an em
   * dash, never as `$0.00`.
   */
  totalCostUsd: number | null;
  createdAt: number;
  updatedAt: number;
}

/** One cursor-paged slice of the governance list. */
export interface GovernanceSessionsPage {
  data: GovernanceSession[];
  firstId: string | null;
  lastId: string | null;
  hasMore: boolean;
}

/**
 * Filters for `GET /v1/governance/sessions`. Every field is optional; an
 * omitted field means "don't constrain on this". `source` and `owner` are
 * repeatable on the wire (one query param per value, OR-ed server-side).
 */
export interface GovernanceSessionsParams {
  source?: string[];
  owner?: string[];
  agentId?: string;
  /** Match sessions whose workspace starts with this path. */
  workspacePrefix?: string;
  /** Epoch seconds — sessions updated at or after this (inclusive bound). */
  updatedAfter?: number;
  /** Epoch seconds — sessions updated at or before this (inclusive bound). */
  updatedBefore?: number;
  searchQuery?: string;
  limit?: number;
  /** Cursor: the id to page forward from (the previous page's `lastId`). */
  after?: string;
  /** Cursor: the id to page backward from (the previous page's `firstId`). */
  before?: string;
  order?: "asc" | "desc";
  sortBy?: "created_at" | "updated_at";
}

/** Default page size — matches the sidebar's conversation list. */
export const GOVERNANCE_PAGE_SIZE = 25;

function sessionFromWire(wire: GovernanceSessionWire): GovernanceSession {
  return {
    id: wire.id,
    title: wire.title ?? null,
    source: wire.source,
    externalSessionId: wire.external_session_id ?? null,
    owner: wire.owner ?? null,
    agentId: wire.agent_id ?? null,
    workspace: wire.workspace ?? null,
    gitBranch: wire.git_branch ?? null,
    itemCount: wire.item_count,
    totalCostUsd: wire.total_cost_usd ?? null,
    createdAt: wire.created_at,
    updatedAt: wire.updated_at,
  };
}

/**
 * Build an {@link ApiError} from a non-OK response, preferring the server's
 * `error.message` / `error.code` over the bare status line. Duplicated from
 * `sessionsApi.ts` (which keeps its copy module-private) rather than widening
 * that module's export surface for one caller.
 */
async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code: string | null = null;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
  } catch {
    // Non-JSON / empty body — keep the status-line fallback.
  }
  return new ApiError(message, res.status, code);
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

/** Serialize the filter set into the route's query string. */
function toQuery(params: GovernanceSessionsParams): URLSearchParams {
  const query = new URLSearchParams();
  for (const value of params.source ?? []) query.append("source", value);
  for (const value of params.owner ?? []) query.append("owner", value);
  if (params.agentId) query.set("agent_id", params.agentId);
  if (params.workspacePrefix) query.set("workspace_prefix", params.workspacePrefix);
  if (params.updatedAfter !== undefined) query.set("updated_after", String(params.updatedAfter));
  if (params.updatedBefore !== undefined) query.set("updated_before", String(params.updatedBefore));
  if (params.searchQuery) query.set("search_query", params.searchQuery);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.after) query.set("after", params.after);
  if (params.before) query.set("before", params.before);
  if (params.order) query.set("order", params.order);
  if (params.sortBy) query.set("sort_by", params.sortBy);
  return query;
}

/**
 * Fetch one session's governance row by id.
 *
 * Carries the provenance the plain session snapshot does not: `owner` and
 * `source`. Admin-only and audited server-side, so opening someone else's
 * transcript is itself a recorded event.
 */
export async function fetchGovernanceSession(sessionId: string): Promise<GovernanceSession> {
  const res = await authenticatedFetch(`/v1/governance/sessions/${encodeURIComponent(sessionId)}`);
  return sessionFromWire(await readJsonOrThrow<GovernanceSessionWire>(res));
}

/**
 * Fetch one page of the server-wide session list.
 *
 * Admin-only: the server 403s a non-admin caller, which surfaces here as an
 * `ApiError` with status 403. Callers page forward by passing the previous
 * page's `lastId` as `after`.
 */
export async function fetchGovernanceSessions(
  params: GovernanceSessionsParams = {},
): Promise<GovernanceSessionsPage> {
  const query = toQuery(params);
  const suffix = query.toString();
  const res = await authenticatedFetch(`/v1/governance/sessions${suffix ? `?${suffix}` : ""}`);
  const wire = await readJsonOrThrow<GovernanceSessionsPageWire>(res);
  return {
    data: wire.data.map(sessionFromWire),
    firstId: wire.first_id,
    lastId: wire.last_id,
    hasMore: wire.has_more,
  };
}

// No sync client here. Importing local sessions is its own surface
// (Settings → Import sessions); governance is a read-only audit view, and
// giving it a write that mutates the session table would put an action
// outside its remit behind an admin-only screen.
