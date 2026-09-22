import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** Transport type for an MCP server entry. */
export type McpTransport =
  "stdio" | "http" | "sse" | "streamable-http" | "streamableHttp" | "unknown";

/** Harness CLI whose MCP config a server belongs to. */
export type McpHarness = "claude" | "codex";

/** Harnesses the API accepts, for rendering a picker. */
export const MCP_HARNESSES: readonly McpHarness[] = ["claude", "codex"] as const;

/** Status of an MCP server as resolved by the host. */
export type McpServerStatus = "resolvable" | "unresolvable" | "configured" | "unknown";

/** One MCP server entry from GET /v1/mcp-config/servers. No secret values. */
export interface McpServerInfo {
  name: string;
  harness: McpHarness;
  transport: McpTransport;
  status: McpServerStatus;
  command?: string;
  args?: string[];
  env_keys?: string[];
  header_keys?: string[];
  url?: string;
}

interface McpServersListWire {
  data: McpServerInfo[];
  host_id: string;
}

/** The server list plus the host it came from. */
export interface McpServersResult {
  servers: McpServerInfo[];
  hostId: string;
}

/**
 * Read the API's error detail, falling back to the status line.
 *
 * FastAPI reports refusals as {"detail": "..."} — surfacing that text is what
 * makes a rejected add actionable ("a server named X already exists") instead
 * of a bare 400.
 */
async function errorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) return body.detail;
  } catch {
    // Non-JSON body — fall through to the status line.
  }
  return `${res.status} ${res.statusText}`;
}

async function fetchMcpServers(): Promise<McpServersResult> {
  const res = await authenticatedFetch("/v1/mcp-config/servers");
  if (!res.ok) throw new Error(await errorDetail(res));
  const body = (await res.json()) as McpServersListWire;
  return { servers: body.data, hostId: body.host_id };
}

/** react-query key for the MCP server list. */
export const MCP_SERVERS_KEY = ["mcp-config-servers"] as const;

/**
 * MCP servers registered with the harness CLIs on the connected host.
 *
 * Host-backed, not server-backed: an MCP server only works if it is registered
 * on the machine where the harness CLI runs, so this reads through the host
 * tunnel. Mutations below invalidate this key.
 */
export function useMcpServers() {
  return useQuery({
    queryKey: MCP_SERVERS_KEY,
    queryFn: fetchMcpServers,
    staleTime: 60_000,
  });
}

/** Definition of a new MCP server. Secret values travel outbound only. */
export interface McpServerDraft {
  harness: McpHarness;
  name: string;
  transport: "stdio" | "http" | "sse";
  /** stdio: the executable to spawn, e.g. "npx". */
  command?: string;
  /** stdio: arguments for the command. */
  args?: string[];
  /** stdio: environment variables. Values are secrets. */
  env?: Record<string, string>;
  /** http/sse: the endpoint URL. */
  url?: string;
  /** http/sse: extra headers, e.g. Authorization. Values are secrets. */
  headers?: Record<string, string>;
}

/** Result of an add/remove — the write landed on the host's harness config. */
export interface McpMutationResult {
  name: string;
  harness: McpHarness;
  host_id: string;
  /** Always true: a running harness reads MCP config only at startup. */
  restart_required: boolean;
  detail: string;
}

async function addMcpServer(draft: McpServerDraft): Promise<McpMutationResult> {
  const res = await authenticatedFetch("/v1/mcp-config/servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      harness: draft.harness,
      name: draft.name,
      transport: draft.transport,
      command: draft.command ?? null,
      args: draft.args ?? [],
      env: draft.env ?? {},
      url: draft.url ?? null,
      headers: draft.headers ?? {},
    }),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
  return (await res.json()) as McpMutationResult;
}

/**
 * Register an MCP server with a harness CLI on the host.
 *
 * Invalidates the server list on success. The new server applies to sessions
 * started after the write — the returned `detail` says so, since a running
 * session will not see it.
 */
export function useAddMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: addMcpServer,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MCP_SERVERS_KEY });
    },
  });
}

async function removeMcpServer(vars: {
  name: string;
  harness: McpHarness;
}): Promise<McpMutationResult> {
  const query = new URLSearchParams({ harness: vars.harness });
  const res = await authenticatedFetch(
    `/v1/mcp-config/servers/${encodeURIComponent(vars.name)}?${query}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(await errorDetail(res));
  return (await res.json()) as McpMutationResult;
}

/** Unregister an MCP server from a harness CLI on the host. */
export function useRemoveMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: removeMcpServer,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MCP_SERVERS_KEY });
    },
  });
}

/** Result of a live usability probe for one MCP server. No secret values. */
export interface McpProbeResult {
  name: string;
  usable: boolean;
  tool_count: number;
  detail: string;
}

async function probeMcpServer(vars: {
  name: string;
  harness: McpHarness;
}): Promise<McpProbeResult> {
  const query = new URLSearchParams({ harness: vars.harness });
  const res = await authenticatedFetch(
    `/v1/mcp-config/servers/${encodeURIComponent(vars.name)}/probe?${query}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await errorDetail(res));
  return (await res.json()) as McpProbeResult;
}

/**
 * Live-probe ONE MCP server for real usability (spawn + MCP handshake).
 *
 * Runs on the host, where the server would actually be spawned, so a pass here
 * means it will work in a session. Unlike the list's PATH-resolvability status,
 * this confirms the server actually starts and speaks MCP. On-demand only —
 * never fired automatically — so loading the list stays cheap.
 */
export function useProbeMcpServer() {
  return useMutation({
    mutationFn: probeMcpServer,
  });
}

// --- Update (edit) an existing MCP server ---

async function updateMcpServer(vars: {
  name: string;
  draft: McpServerDraft;
}): Promise<McpMutationResult> {
  const res = await authenticatedFetch(`/v1/mcp-config/servers/${encodeURIComponent(vars.name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      harness: vars.draft.harness,
      name: vars.draft.name,
      transport: vars.draft.transport,
      command: vars.draft.command ?? null,
      args: vars.draft.args ?? [],
      env: vars.draft.env ?? {},
      url: vars.draft.url ?? null,
      headers: vars.draft.headers ?? {},
    }),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
  return (await res.json()) as McpMutationResult;
}

/** Update an existing MCP server's configuration on the host. */
export function useUpdateMcpServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateMcpServer,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MCP_SERVERS_KEY });
    },
  });
}

// --- Raw config file read/write (download/upload) ---

/** Raw config file content from the host. */
// No raw config file hooks. The server exposes no endpoint for the file's
// text: it would return every server's env values and Authorization headers,
// which this surface never hands back. Editing is per-server (add / update /
// remove).
