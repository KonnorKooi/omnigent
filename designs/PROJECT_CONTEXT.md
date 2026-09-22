# Design: Project Context (per-project context repository + graph)

- Status: Draft — approved for implementation
- Author: Konnor Kooi
- Related: [`designs/PROJECTS_PRD.md`](./PROJECTS_PRD.md) §8.2 (project memory), §8.3 (project context), §9 (owner-private)

## 1. Summary

Give every Project a **context repository**: a folder of human-readable
markdown (plus derived graphs) that seeds and informs every session started in
that project, **regardless of harness**. The project gets a **Context page**
with three sections — **Files** (view/edit md), **Graph** (knowledge + code
graph), and an **Update context** button that refreshes derived data and
*proposes* edits for human review.

Goals, in priority order:

1. **Effective** — small, stable always-loaded context; everything else is
   fetched just-in-time through tools.
2. **Updates easily** — one button; changes are proposed as diffs, never
   silently rewritten.
3. **Interpretable** — plain markdown in a git repo; you can read, edit, diff,
   and open it in Obsidian. The UI shows exactly what a session receives.
4. **Harness-agnostic** — tools ride Omnigent's existing MCP relay, so Claude,
   Codex, Cursor, OpenCode, Kiro, Qwen, Hermes, Antigravity, and ACP harnesses
   all get them.

Non-goals (v1): embeddings/vector search, multi-user sharing of context,
remote/sandbox hosts reading the context repo, automatic unreviewed memory
writes.

## 2. Evidence behind the shape

| Finding | Design consequence |
|---|---|
| Context rot: quality degrades as input grows (Chroma, 18 models) | Always-loaded context is capped and small |
| Anthropic: hybrid — small upfront file + just-in-time tools beats RAG for Claude Code | `system/` injected; `wiki/` + graph via tools |
| *Evaluating AGENTS.md* (arXiv 2602.11988): overviews don't help and cost +20%; instructions are followed | `system/` holds **non-standard rules only**; overviews live in `wiki/` |
| CodeCompass (2602.20048): graph tools +23pp on hidden dependencies, but 58% of runs never call them | Injected text explicitly directs agents to the tools |
| Codebase-Memory (2603.27277): graph = 10x fewer tokens but lower answer quality (83% vs 92%) | Graph is a navigation aid, not the source of truth |
| ACE (2510.04618): iterative rewriting causes context collapse | Updates are incremental, per-file proposals with review |
| Letta Context Repositories / Karpathy LLM Wiki / Agent Skills | Git-backed md tree, frontmatter descriptions, progressive disclosure |
| Manus: KV-cache hit rate dominates cost | Injected prefix is deterministic (sorted, no timestamps) |
| Meta-Harness (2603.28052): optimization needs raw traces | Log what context each session loaded and read |

## 3. Storage model

**Plain files, git-versioned, on the server's machine.** A project's config
points at a directory:

```jsonc
// project.config (opaque JSON, see PROJECTS_PRD §8)
{
  "context": {
    "path": "/home/me/context/projects/timescale",   // required, absolute (~ expanded)
    "profile_path": "/home/me/context/me",           // optional shared "about me" dir
    "repo_path": "/home/me/code/research/Timescale-Autoencoder" // optional; enables code graph
  }
}
```

Directory layout (created by **Initialize**):

```
<path>/
  system/        always injected; SMALL; non-standard rules & conventions
  wiki/          on demand; one topic per file; frontmatter `description:`
  raw/           immutable sources (papers, notes, exports); read-only in UI
  graph/         derived; graphify output (graph.json, GRAPH_REPORT.md)
  .proposals/    pending Update-context proposals (git-ignored)
  CONTEXT.md     short schema/instructions for curators (what goes where)
```

Every `wiki/*.md` and `system/*.md` file may start with YAML frontmatter:

```markdown
---
description: One line used in the injected index and search results.
---
```

Git: if `<path>` (or an ancestor) is a git repo, accepted edits and proposal
applications are committed with a descriptive message. If not, everything
still works; History shows "not versioned".

**Limitation (documented, accepted):** files live where the *server* runs.
Under omnidev / local single-machine use this is the same machine as the host.
Remote hosts and managed sandboxes do not read the directory; they reach the
content through tools, which proxy to the server.

## 4. Backend

### 4.1 `ContextService` (`omnigent/context/`)

Pure-Python, no DB. Constructed per request from a project's `context` config.

- `resolve(rel_path)` — normalises and **rejects traversal**, absolute paths,
  and symlinks escaping the root. Every file op goes through it.
- `tree()` — files under `system/ wiki/ raw/ graph/` with size, mtime,
  frontmatter description, `always_loaded` flag.
- `read(rel)` / `write(rel, content, base_sha)` — md/txt only for writes;
  `base_sha` (sha256 of the content the client loaded) gives optimistic
  concurrency → 409 on mismatch. Writes to `raw/` and `graph/` are refused.
- `create(rel)` / `delete(rel)` / `rename(rel, new_rel)` — md under
  `system/` and `wiki/` only.
- `search(query, limit)` — case-insensitive substring/token search over md
  files, returns `path, line, snippet`. (No embeddings in v1.)
- `injected_context()` — deterministic text: profile files, then
  `system/*.md` (sorted), then a **wiki index** (`path — description`), then a
  fixed tool-usage note. Capped (default 12k chars); truncation is explicit.
  Returns text + char/token estimate.
- `knowledge_graph()` — nodes = md files, edges = `[[wikilinks]]` and relative
  md links; computed on demand.
- `code_graph(limit)` — loads `graph/graph.json` (graphify node-link format:
  `nodes[]`, `links[]` with `source/target/relation`, node `label`,
  `community`, `source_file`, `source_location`); capped for the UI
  (highest-degree nodes first) with a `truncated` flag.
- `graph_query(question, budget)` / `graph_neighbors(node, depth)` — use the
  graphify library in-process when importable; otherwise a simple label-match
  + BFS over `graph.json`. Loaded graphs are cached by `(path, mtime)`.
- `history(limit)` — `git log` for the path (subject, sha, date, files).

### 4.2 REST (`omnigent/server/routes/project_context.py`)

All routes are **owner-scoped** through the project store (`get(project_id,
user_id=...)` → 404 if not owned), consistent with PRD §9. Registered under
`/v1` in `app.py`.

| Method & path | Purpose |
|---|---|
| `GET  /projects/{id}/context` | status: configured?, paths, exists, git?, graphify available?, tree, injected size |
| `POST /projects/{id}/context/init` | scaffold directories + `CONTEXT.md` (idempotent) |
| `GET  /projects/{id}/context/files?path=` | read one file (+ sha) |
| `PUT  /projects/{id}/context/files?path=` | write `{content, base_sha}` → commit if git |
| `POST /projects/{id}/context/files` | create `{path, content}` |
| `DELETE /projects/{id}/context/files?path=` | delete |
| `GET  /projects/{id}/context/search?q=` | search |
| `GET  /projects/{id}/context/preview` | exact injected text + size |
| `GET  /projects/{id}/context/graph?kind=knowledge\|code&limit=` | nodes/edges for UI |
| `POST /projects/{id}/context/update` | start update job → `{job_id}` |
| `GET  /projects/{id}/context/jobs/{job_id}` | job status/log |
| `GET  /projects/{id}/context/proposals` | pending proposals |
| `POST /projects/{id}/context/proposals/{pid}/apply` / `reject` | review |
| `GET  /projects/{id}/context/history` | git log |

Session-scoped variants used by tools (resolve session → project, then the
same service; 404 when the session has no project or no context):
`GET /sessions/{sid}/context/{search,files,graph/query,graph/neighbors,list}`.
Access follows existing session read permission, but context is returned
**only to the project owner** (a shared-session recipient gets 404 — PRD §9.4).

Update config via the existing `updateProjectConfig`; the `context` key is
validated server-side (absolute path, no NUL, etc.) when used.

### 4.3 Built-in tools (every harness)

Every native harness already launches Omnigent's `serve-mcp` relay, whose tool
calls dispatch in `omnigent/runner/tool_dispatch.py` with the session's
`conversation_id` and a server client (see `sys_session_rename`). Add
framework-owned tools registered in `ToolManager` for all sessions, which call
the session-scoped REST routes:

| Tool | Args | Returns |
|---|---|---|
| `context_list` | — | wiki/system index (path, description) |
| `context_read` | `path`, optional `offset/limit` lines | file content |
| `context_search` | `query`, `limit` | snippets |
| `graph_query` | `question`, `budget` | relevant nodes/edges text |
| `graph_neighbors` | `node`, `depth` (≤2) | neighbors with relation + source location |

Outputs are paginated/truncated with explicit notices. When a session has no
project context the tools return a clear, non-error message. Tools are
read-only — agents *propose* memory via Update context, not direct writes.
Pre-approve them where harness permission lists exist (cf.
`_FRAMEWORK_APPROVED_TOOLS` in codex app_server).

### 4.4 Start-of-session injection

`injected_context()` is appended to harness startup instructions when the
session belongs to a project with context:

- **Claude native:** `append_system_prompt` in
  `omnigent/runner/native/orchestration.py` (joined with existing parts).
- **Codex native:** `developer_instructions` in the session's private config.
- Other harnesses: follow-up, one at a time; tools already work there.

The injected note always ends with: *"Project context tools are available:
context_list, context_read, context_search, graph_query, graph_neighbors. Check
them before exploring broadly or when the task touches project history,
decisions, experiments, or cross-file structure."*

### 4.5 Update context job

In-process async job (single-user local scope), one at a time per project,
status polled via `jobs/{id}`. Steps, each logged and individually skippable:

1. **Code graph** — if `repo_path` set and `graphify` on PATH:
   `graphify extract <repo_path> --code-only --out <tmp>` then
   `graphify cluster-only <tmp> --no-label`, copy `graphify-out/*` into
   `<path>/graph/`. (Honors the repo's `.graphifyignore`.)
2. **Proposals** — gather inputs since the last update (`.last_update` stamp):
   new/changed files in `raw/` and transcripts of the project's sessions
   updated since then. Run a curator LLM pass that emits **per-file
   proposals**: `{path, action: create|edit, new_content, rationale,
   sources[]}` into `.proposals/<pid>.json`. Rules for the curator: edit
   incrementally, never delete information without a rationale, keep
   `system/` minimal, cite sources. Mechanism: reuse Omnigent's own session
   launch (a hidden/system session in the project with a curator prompt) so it
   is harness-agnostic; if that proves too heavy for v1, a headless
   `claude -p` subprocess behind an interface is acceptable.
3. Write `.last_update`.

Apply = write file(s) through `ContextService.write` + git commit
`context: apply proposal <pid> — <rationale>`. Reject = delete proposal.

### 4.6 Tracing

Record per session (session_state or a small table — implementer's choice):
the injected text hash + size, and each context tool call (tool, args, paths
or node ids returned). Powers the session Context tab and later evaluation.

## 5. Frontend

### 5.1 Project Context page — `/projects/:projectId/context`

Entry points: project row kebab menu → "Context", and a link in the project
settings dialog. Header shows project name, context path, git status, and
**Update context** button (disabled while a job runs; shows progress/log).

If the project has no `context` config: an empty state with a form (path,
profile path, repo path) + **Initialize**.

Sections (tabs):

1. **Files** — left tree (`system/` badged "always loaded", `wiki/`, `raw/`
   read-only, `graph/` read-only); right pane toggles **Preview**
   (react-markdown, wikilinks clickable) / **Edit** (Monaco, markdown mode;
   Save with `base_sha`, conflict banner on 409). New / rename / delete for
   md under `system/` and `wiki/`. A **"What sessions see"** view renders
   `/preview` with its size.
2. **Graph** — toggle **Knowledge** (md link graph) / **Code** (graphify).
   Force-directed canvas; colour by community (code) or folder (knowledge);
   search box; clicking a node opens the md file (knowledge) or shows
   `source_file:location` + neighbors (code). Handle ~1–2k nodes; show the
   truncation flag. Prefer existing deps (`@xyflow/react`) only if it performs
   at this size; otherwise add one small force-graph dependency and justify it.
3. **Proposals** — list pending proposals; per-file side-by-side diff
   (Monaco diff editor); Apply / Reject.
4. **History** — git log list.

### 5.2 Session Context tab

New tab in `web/src/shell/WorkspacePanel.tsx` (alongside Files, Changes,
GitHub, Subagents, Browser), shown when the session's project has context:
injected text (collapsible) + chronological list of context tool calls with
the files/nodes returned, each linking to the project Context page.

## 6. Security & privacy

- Path confinement on every file op (traversal, absolute paths, symlink escape).
- Owner-only access; shared-session recipients never see project context.
- Writes limited to md under `system/` and `wiki/`; size cap per file (1 MB).
- Subprocesses (`graphify`, `git`) invoked with argv lists, never a shell,
  with timeouts.
- Don't return `.env*`-like files from `raw/` listings' contents (list only).

## 7. Phases

1. **Service + REST + config** — ContextService, routes, init, tests.
2. **Tools + injection** — five built-ins through the relay; Claude and Codex
   injection; tests.
3. **Context page** — Files (preview/edit/preview-injected) + Graph + History.
4. **Update context** — job runner, code graph refresh, proposals + review UI.
5. **Session Context tab + tracing.**

Each phase ships green: backend tests (`uv run pytest`), `ruff`, frontend
`tsc`, `oxlint`, and vitest for new components.

## 8. Open questions

- Curator mechanism for proposals (Omnigent session vs headless CLI) — decide
  in phase 4 after measuring overhead.
- Whether to also expose proposals as an agent tool (`context_propose`) so a
  session can suggest a memory at the end of a task.
- Remote hosts: sync the context dir to hosts, or keep tools-only access.
