// Typed client for project context repositories
// (`omnigent/server/routes/project_context.py`, designs/PROJECT_CONTEXT.md).
//
// A project's `config.context` points at a directory of markdown on the
// server's machine. These endpoints read/edit that directory, render the
// startup text sessions receive, expose the knowledge/code graphs, and run the
// "Update context" job whose output is reviewed as per-file proposals.
//
// Wire shapes are kept snake_case (like projectsApi) — the page reads them
// directly and there is no second consumer that would benefit from a mapping.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/** The `context` block stored in a project's config. */
export interface ProjectContextConfig {
  /** Absolute context repository directory (`~` allowed). */
  path: string;
  /** Optional shared "about me" directory injected before `system/`. */
  profile_path?: string | null;
  /** Optional source repository; enables the code graph refresh. */
  repo_path?: string | null;
}

export interface ContextFileEntry {
  path: string;
  size: number;
  mtime: number;
  description: string | null;
  always_loaded: boolean;
  writable: boolean;
  readable: boolean;
}

export interface ContextTree {
  files: ContextFileEntry[];
  truncated: boolean;
}

export interface ContextStatusUnconfigured {
  configured: false;
  project_id: string;
  project_name: string;
}

export interface ContextStatusConfigured {
  configured: true;
  project_id: string;
  project_name: string;
  config: ProjectContextConfig;
  exists: boolean;
  initialized: boolean;
  git: { toplevel: string } | null;
  graphify_available: boolean;
  has_code_graph: boolean;
  tree: ContextTree;
  injected: { chars: number; approx_tokens: number; truncated: boolean } | null;
  last_update: number | null;
  pending_proposals?: number;
  active_job?: ContextJob | null;
}

export type ContextStatus = ContextStatusUnconfigured | ContextStatusConfigured;

export interface ContextFile {
  path: string;
  content: string | null;
  sha: string | null;
  size: number;
  writable: boolean;
  binary: boolean;
  truncated: boolean;
}

export interface ContextWriteResult {
  path: string;
  sha: string;
  size: number;
  commit: string | null;
}

export interface InjectedContext {
  text: string;
  chars: number;
  approx_tokens: number;
  truncated: boolean;
  sha256: string;
  files: string[];
  /** Profile files left out because the requesting harness loads them itself. */
  skipped?: string[];
  /** Per harness, the profile files it loads natively and so is not sent. */
  skipped_by_harness?: Record<string, string[]>;
}

export interface ContextSearchResult {
  query: string;
  results: { path: string; line: number; snippet: string; score: number }[];
  truncated: boolean;
}

export interface GraphNode {
  id: string;
  label: string;
  /** Knowledge graph: top folder. */
  group?: string;
  description?: string | null;
  /** Code graph fields. */
  community?: number | null;
  community_name?: string | null;
  source_file?: string | null;
  source_location?: string | null;
  degree?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation: string;
}

export interface ContextGraph {
  kind: "knowledge" | "code";
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  available?: boolean;
  total_nodes?: number;
  total_edges?: number;
}

export interface ContextCommit {
  sha: string;
  subject: string;
  timestamp: number;
  author: string;
  files: string[];
}

export interface ContextHistory {
  versioned: boolean;
  commits: ContextCommit[];
}

export type ContextJobStatus = "running" | "succeeded" | "failed";
export type ContextJobStepStatus = "pending" | "running" | "succeeded" | "skipped" | "failed";

export interface ContextJob {
  id: string;
  project_id: string;
  status: ContextJobStatus;
  started_at: number;
  finished_at: number | null;
  steps: { name: string; status: ContextJobStepStatus; detail: string | null }[];
  log: string[];
  proposals_created: number;
  error: string | null;
}

export interface ContextProposal {
  id: string;
  path: string;
  action: "create" | "edit";
  new_content: string;
  rationale: string;
  sources: string[];
  base_sha: string | null;
  created_at: number;
  job_id: string | null;
  /** Content currently on disk (null when the file does not exist). */
  current_content: string | null;
  /** True when the file changed since the proposal was made. */
  stale: boolean;
}

/** One context tool call (or the injection) recorded for a session. */
export interface ContextTraceEvent {
  ts: number;
  kind: "injected" | "tool";
  tool?: string;
  args?: Record<string, unknown>;
  paths?: string[];
  node_ids?: string[];
  sha256?: string;
  chars?: number;
}

export interface SessionContextTrace {
  project_id: string;
  project_name: string;
  injected: InjectedContext;
  events: ContextTraceEvent[];
}

function projectBase(projectId: string): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/context`;
}

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await authenticatedFetch(url, init);
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

/** Context status for a project (configured flag, tree, injected size…). */
export function fetchContextStatus(projectId: string): Promise<ContextStatus> {
  return getJson(projectBase(projectId));
}

/** Scaffold `system/ wiki/ raw/ graph/` + `CONTEXT.md` (idempotent). */
export function initContext(projectId: string): Promise<ContextStatus & { created: string[] }> {
  return getJson(`${projectBase(projectId)}/init`, jsonInit("POST"));
}

export function fetchContextFile(projectId: string, path: string): Promise<ContextFile> {
  return getJson(`${projectBase(projectId)}/files?path=${encodeURIComponent(path)}`);
}

/**
 * Save a file. `baseSha` is the sha of the content the editor loaded; the
 * server answers 409 (thrown as ApiError with status 409) when it changed.
 */
export function saveContextFile(
  projectId: string,
  path: string,
  content: string,
  baseSha: string | null,
): Promise<ContextWriteResult> {
  return getJson(
    `${projectBase(projectId)}/files?path=${encodeURIComponent(path)}`,
    jsonInit("PUT", { content, base_sha: baseSha }),
  );
}

export function createContextFile(
  projectId: string,
  path: string,
  content = "",
): Promise<ContextWriteResult> {
  return getJson(`${projectBase(projectId)}/files`, jsonInit("POST", { path, content }));
}

export function renameContextFile(
  projectId: string,
  path: string,
  newPath: string,
): Promise<{ path: string; old_path: string }> {
  return getJson(
    `${projectBase(projectId)}/files/rename`,
    jsonInit("POST", { path, new_path: newPath }),
  );
}

export function deleteContextFile(projectId: string, path: string): Promise<void> {
  return getJson(
    `${projectBase(projectId)}/files?path=${encodeURIComponent(path)}`,
    jsonInit("DELETE"),
  );
}

export function searchContext(projectId: string, q: string): Promise<ContextSearchResult> {
  return getJson(`${projectBase(projectId)}/search?q=${encodeURIComponent(q)}&limit=50`);
}

/** The exact startup text sessions in this project receive. */
export function fetchInjectedPreview(projectId: string): Promise<InjectedContext> {
  return getJson(`${projectBase(projectId)}/preview`);
}

export function fetchContextGraph(
  projectId: string,
  kind: "knowledge" | "code",
  limit = 1500,
): Promise<ContextGraph> {
  return getJson(`${projectBase(projectId)}/graph?kind=${kind}&limit=${limit}`);
}

export function fetchContextHistory(projectId: string): Promise<ContextHistory> {
  return getJson(`${projectBase(projectId)}/history?limit=100`);
}

/** Start the Update context job. 409 when one is already running. */
export function startContextUpdate(
  projectId: string,
  options: { code_graph?: boolean; proposals?: boolean } = {},
): Promise<ContextJob> {
  return getJson(`${projectBase(projectId)}/update`, jsonInit("POST", options));
}

export function fetchContextJob(projectId: string, jobId: string): Promise<ContextJob> {
  return getJson(`${projectBase(projectId)}/jobs/${encodeURIComponent(jobId)}`);
}

export function fetchContextProposals(
  projectId: string,
): Promise<{ proposals: ContextProposal[] }> {
  return getJson(`${projectBase(projectId)}/proposals`);
}

export function applyContextProposal(
  projectId: string,
  proposalId: string,
): Promise<{ path: string; commit: string | null }> {
  return getJson(
    `${projectBase(projectId)}/proposals/${encodeURIComponent(proposalId)}/apply`,
    jsonInit("POST"),
  );
}

export function rejectContextProposal(projectId: string, proposalId: string): Promise<void> {
  return getJson(
    `${projectBase(projectId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
    jsonInit("POST"),
  );
}

/** Injected context + recorded context tool calls for one session. */
export function fetchSessionContextTrace(sessionId: string): Promise<SessionContextTrace> {
  return getJson(`/v1/sessions/${encodeURIComponent(sessionId)}/context/trace`);
}
