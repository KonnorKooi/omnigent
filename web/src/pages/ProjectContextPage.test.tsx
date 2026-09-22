// Tests for the project Context page. The API module seam is mocked (the
// server routes are covered by tests/server/routes/test_project_context.py);
// this suite owns the empty-state config flow, the file tree and editor tabs,
// the "What sessions see" view, the graph panel, and the proposals/history tabs.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ContextFileEntry,
  ContextJob,
  ContextProposal,
  ContextStatusConfigured,
  ContextStatusUnconfigured,
} from "@/lib/projectContextApi";
import * as api from "@/lib/projectContextApi";
import * as projectsApi from "@/lib/projectsApi";
import { ApiError } from "@/lib/sessionsApi";
import { ProjectContextPage } from "./ProjectContextPage";

vi.mock("@/lib/projectContextApi", () => ({
  fetchContextStatus: vi.fn(),
  initContext: vi.fn(),
  fetchContextFile: vi.fn(),
  saveContextFile: vi.fn(),
  createContextFile: vi.fn(),
  renameContextFile: vi.fn(),
  deleteContextFile: vi.fn(),
  fetchInjectedPreview: vi.fn(),
  fetchContextGraph: vi.fn(),
  fetchContextHistory: vi.fn(),
  startContextUpdate: vi.fn(),
  fetchContextJob: vi.fn(),
  fetchContextProposals: vi.fn(),
  applyContextProposal: vi.fn(),
  rejectContextProposal: vi.fn(),
}));
vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  updateProjectConfig: vi.fn(),
}));
// Monaco does not run under jsdom; a textarea stands in for the editor.
vi.mock("@/components/projectContext/ContextMonaco", () => ({
  ContextMarkdownEditor: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (v: string) => void;
  }) => <textarea aria-label="editor" value={value} onChange={(e) => onChange(e.target.value)} />,
  ContextDiffEditor: ({ original, modified }: { original: string; modified: string }) => (
    <div data-testid="diff">
      <pre data-testid="diff-original">{original}</pre>
      <pre data-testid="diff-modified">{modified}</pre>
    </div>
  ),
}));
vi.mock("@/components/projectContext/ForceGraphCanvas", () => ({
  ForceGraphCanvas: ({
    nodes,
    highlighted,
    onSelect,
  }: {
    nodes: { id: string; label: string }[];
    highlighted: Set<string>;
    onSelect: (n: unknown) => void;
  }) => (
    <ul data-testid="graph">
      {nodes.map((n) => (
        <li key={n.id}>
          <button type="button" data-lit={highlighted.has(n.id)} onClick={() => onSelect(n)}>
            {n.label}
          </button>
        </li>
      ))}
    </ul>
  ),
}));

function entry(path: string, overrides: Partial<ContextFileEntry> = {}): ContextFileEntry {
  return {
    path,
    size: 10,
    mtime: 0,
    description: null,
    always_loaded: path.startsWith("system/"),
    writable: path.startsWith("system/") || path.startsWith("wiki/"),
    readable: true,
    ...overrides,
  };
}

const CONFIGURED: ContextStatusConfigured = {
  configured: true,
  project_id: "p1",
  project_name: "Timescale",
  config: { path: "/ctx/timescale" },
  exists: true,
  initialized: true,
  git: { toplevel: "/ctx" },
  graphify_available: true,
  has_code_graph: true,
  tree: {
    files: [entry("system/rules.md"), entry("wiki/exp.md"), entry("raw/.env", { readable: false })],
    truncated: false,
  },
  injected: { chars: 1200, approx_tokens: 300, truncated: false },
  last_update: null,
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/p1/context"]}>
        <Routes>
          <Route path="/projects/:projectId/context" element={<ProjectContextPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(api.fetchContextGraph).mockResolvedValue({
    kind: "knowledge",
    truncated: false,
    nodes: [],
    edges: [],
  });
  vi.mocked(api.fetchContextStatus).mockResolvedValue(CONFIGURED);
  vi.mocked(api.fetchContextFile).mockImplementation(async (_p, path) => ({
    path,
    content: path === "wiki/exp.md" ? "---\ndescription: Exps\n---\nSee [[rules]]." : "Use uv.",
    sha: `sha-${path}`,
    size: 10,
    writable: true,
    binary: false,
    truncated: false,
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProjectContextPage", () => {
  it("configures an unconfigured project by merging config and initialising", async () => {
    const unconfigured: ContextStatusUnconfigured = {
      configured: false,
      project_id: "p1",
      project_name: "Timescale",
    };
    vi.mocked(api.fetchContextStatus).mockResolvedValueOnce(unconfigured);
    vi.mocked(projectsApi.getProject).mockResolvedValue({
      id: "p1",
      name: "Timescale",
      config: { workspace: "/code/ts" },
    });
    vi.mocked(projectsApi.updateProjectConfig).mockResolvedValue({ id: "p1", name: "Timescale" });
    vi.mocked(api.initContext).mockResolvedValue({ ...CONFIGURED, created: [] });

    renderPage();
    fireEvent.change(await screen.findByLabelText("Context folder"), {
      target: { value: "~/context/projects/timescale" },
    });
    fireEvent.change(screen.getByLabelText("Code repository"), {
      target: { value: "~/code/ts" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Initialize" }));

    await waitFor(() => expect(api.initContext).toHaveBeenCalledWith("p1"));
    expect(projectsApi.updateProjectConfig).toHaveBeenCalledWith("p1", {
      workspace: "/code/ts",
      context: { path: "~/context/projects/timescale", repo_path: "~/code/ts" },
    });
  });

  it("opens the default file on load and shows the nested tree with descriptions", async () => {
    vi.mocked(api.fetchContextStatus).mockResolvedValue({
      ...CONFIGURED,
      tree: {
        files: [...CONFIGURED.tree.files, entry("wiki/a/deep.md", { description: "Deep notes" })],
        truncated: false,
      },
    });
    renderPage();
    expect(await screen.findByText("Timescale · Context")).toBeTruthy();
    expect(screen.getByText("/ctx/timescale")).toBeTruthy();
    await waitFor(() => expect(api.fetchContextFile).toHaveBeenCalledWith("p1", "system/rules.md"));
    expect(await screen.findByLabelText("editor")).toBeTruthy();
    const tree = screen.getByTestId("context-file-tree");
    expect(within(tree).getByText("always loaded")).toBeTruthy();
    expect(within(tree).getByRole("button", { name: /deep\.md/ }).textContent).toContain(
      "Deep notes",
    );
    fireEvent.click(within(tree).getByRole("button", { name: /^a$/ }));
    expect(within(tree).queryByRole("button", { name: /deep\.md/ })).toBeNull();
  });

  it("previews markdown with clickable wikilinks that open new tabs", async () => {
    renderPage();
    const tree = await screen.findByTestId("context-file-tree");
    fireEvent.click(within(tree).getByRole("button", { name: /exp\.md/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Preview/ }));
    const preview = await screen.findByTestId("context-preview");
    expect(within(preview).getByText("description: Exps")).toBeTruthy();
    fireEvent.click(within(preview).getByRole("button", { name: "rules" }));
    expect(await screen.findByRole("button", { name: "Close system/rules.md" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close wiki/exp.md" })).toBeTruthy();
  });

  it("does not fetch secret-looking files", async () => {
    vi.mocked(api.fetchContextStatus).mockResolvedValue({
      ...CONFIGURED,
      tree: { files: [entry("raw/.env", { readable: false })], truncated: false },
    });
    renderPage();
    const tree = await screen.findByTestId("context-file-tree");
    fireEvent.click(within(tree).getByRole("button", { name: /\.env/ }));
    expect(await screen.findByText(/looks like it holds secrets/)).toBeTruthy();
    expect(api.fetchContextFile).not.toHaveBeenCalled();
  });

  it("saves with Ctrl+S using the loaded sha and shows a conflict banner on 409", async () => {
    vi.mocked(api.saveContextFile).mockRejectedValueOnce(new ApiError("changed", 409, "conflict"));
    renderPage();
    fireEvent.change(await screen.findByLabelText("editor"), { target: { value: "Use uv run." } });
    expect(screen.getAllByLabelText("unsaved").length).toBeGreaterThan(0);
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    await waitFor(() =>
      expect(api.saveContextFile).toHaveBeenCalledWith(
        "p1",
        "system/rules.md",
        "Use uv run.",
        "sha-system/rules.md",
      ),
    );
    expect((await screen.findByRole("alert")).textContent).toMatch(/changed on disk/);
  });

  it("asks before closing a tab with unsaved edits", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    fireEvent.change(await screen.findByLabelText("editor"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Close system/rules.md" }));
    expect(confirm).toHaveBeenCalled();
    expect(screen.getByLabelText("editor")).toBeTruthy();
    confirm.mockRestore();
  });

  it("renders what sessions see", async () => {
    vi.mocked(api.fetchInjectedPreview).mockResolvedValue({
      text: "# Project context\nUse uv.",
      chars: 25,
      approx_tokens: 7,
      truncated: false,
      sha256: "abc",
      files: ["system/rules.md"],
      skipped_by_harness: { "claude-native": ["profile/CLAUDE.md"] },
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /What sessions see/ }));
    const view = await screen.findByTestId("context-injected-view");
    expect(view.textContent).toContain("Use uv.");
    expect(view.textContent).toContain("25 chars");
    expect(within(view).getByTestId("context-skipped").textContent).toBe(
      "profile/CLAUDE.md: skipped for claude-native sessions, which load it natively.",
    );
  });

  it("shows the code graph on load, highlights search matches and shows neighbours", async () => {
    vi.mocked(api.fetchContextGraph).mockImplementation(async (_p, kind) =>
      kind === "code"
        ? {
            kind: "code",
            available: true,
            truncated: true,
            total_nodes: 900,
            nodes: [
              { id: "a", label: "compute_psnr", source_file: "eval/m.py", source_location: "L3" },
              { id: "b", label: "Trainer", source_file: "train.py" },
            ],
            edges: [{ source: "b", target: "a", relation: "calls" }],
          }
        : { kind: "knowledge", truncated: false, nodes: [], edges: [] },
    );
    renderPage();
    const panel = await screen.findByTestId("context-graph-panel");
    const graph = await within(panel).findByTestId("graph");
    expect(api.fetchContextGraph).toHaveBeenCalledWith("p1", "code");
    expect(within(panel).getByText(/showing top 2 of 900 by degree/)).toBeTruthy();

    fireEvent.change(within(panel).getByLabelText("Search graph"), { target: { value: "psnr" } });
    expect(within(graph).getByText("compute_psnr").getAttribute("data-lit")).toBe("true");
    expect(within(graph).getByText("Trainer").getAttribute("data-lit")).toBe("false");

    fireEvent.click(within(graph).getByText("compute_psnr"));
    const details = await screen.findByTestId("context-graph-details");
    expect(details.textContent).toContain("eval/m.py:L3");
    expect(details.textContent).toContain("← calls Trainer");
  });

  it("opens the knowledge graph without a code graph and opens clicked files", async () => {
    vi.mocked(api.fetchContextStatus).mockResolvedValue({ ...CONFIGURED, has_code_graph: false });
    vi.mocked(api.fetchContextGraph).mockResolvedValue({
      kind: "knowledge",
      truncated: false,
      nodes: [{ id: "wiki/exp.md", label: "exp", group: "wiki" }],
      edges: [],
    });
    renderPage();
    const graph = await within(await screen.findByTestId("context-graph-panel")).findByTestId(
      "graph",
    );
    expect(api.fetchContextGraph).toHaveBeenCalledWith("p1", "knowledge");
    fireEvent.click(within(graph).getByText("exp"));
    expect(await screen.findByRole("button", { name: "Close wiki/exp.md" })).toBeTruthy();
  });

  it("opens graph/graph.json as a graph and can hide the side panel", async () => {
    vi.mocked(api.fetchContextStatus).mockResolvedValue({
      ...CONFIGURED,
      tree: {
        files: [...CONFIGURED.tree.files, entry("graph/graph.json")],
        truncated: false,
      },
    });
    vi.mocked(api.fetchContextGraph).mockResolvedValue({
      kind: "code",
      available: true,
      truncated: false,
      nodes: [{ id: "a", label: "compute_psnr" }],
      edges: [],
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Hide graph" }));
    expect(screen.queryByTestId("context-graph-panel")).toBeNull();
    const tree = screen.getByTestId("context-file-tree");
    fireEvent.click(within(tree).getByRole("button", { name: /graph\.json/ }));
    const editor = screen.getByTestId("context-editor");
    expect(await within(editor).findByTestId("graph")).toBeTruthy();
    expect(api.fetchContextFile).not.toHaveBeenCalledWith("p1", "graph/graph.json");
    fireEvent.click(screen.getByRole("button", { name: "Show graph" }));
    expect(screen.getByTestId("context-graph-panel")).toBeTruthy();
  });

  it("history tab explains an unversioned folder", async () => {
    vi.mocked(api.fetchContextHistory).mockResolvedValue({ versioned: false, commits: [] });
    renderPage();
    fireEvent.mouseDown(await screen.findByRole("tab", { name: "History" }), { button: 0 });
    expect(await screen.findByTestId("context-history-unversioned")).toBeTruthy();
  });

  it("starts Update context and shows step progress when the job finishes", async () => {
    const running: ContextJob = {
      id: "job1",
      project_id: "p1",
      status: "running",
      started_at: 1,
      finished_at: null,
      steps: [
        { name: "code_graph", status: "running", detail: null },
        { name: "proposals", status: "pending", detail: null },
        { name: "stamp", status: "pending", detail: null },
      ],
      log: ["building code graph"],
      proposals_created: 0,
      error: null,
    };
    vi.mocked(api.startContextUpdate).mockResolvedValue(running);
    vi.mocked(api.fetchContextJob).mockResolvedValue({
      ...running,
      status: "succeeded",
      finished_at: 2,
      steps: [
        { name: "code_graph", status: "succeeded", detail: "updated 2 file(s) in graph/" },
        { name: "proposals", status: "succeeded", detail: "1 proposal(s) ready for review" },
        { name: "stamp", status: "succeeded", detail: "recorded .last_update" },
      ],
      proposals_created: 1,
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Update context" }));
    await waitFor(() => expect(api.startContextUpdate).toHaveBeenCalledWith("p1"));
    const log = await screen.findByTestId("context-job-log");
    expect(log.textContent).toContain("building code graph");
    await waitFor(() => expect(log.textContent).toContain("1 proposal(s) ready for review"), {
      timeout: 4000,
    });
    expect(screen.getByRole("button", { name: "Update context" })).not.toBeDisabled();
  });

  it("reviews a proposal as a diff and applies it", async () => {
    const proposal: ContextProposal = {
      id: "abc",
      path: "wiki/exp.md",
      action: "edit",
      new_content: "new body",
      rationale: "Record the result",
      sources: ["session:conv_1"],
      base_sha: "sha",
      created_at: 1,
      job_id: "job1",
      current_content: "old body",
      stale: false,
    };
    vi.mocked(api.fetchContextStatus).mockResolvedValue({ ...CONFIGURED, pending_proposals: 1 });
    vi.mocked(api.fetchContextProposals)
      .mockResolvedValueOnce({ proposals: [proposal] })
      .mockResolvedValue({ proposals: [] });
    vi.mocked(api.applyContextProposal).mockResolvedValue({ path: "wiki/exp.md", commit: null });
    renderPage();
    fireEvent.mouseDown(await screen.findByRole("tab", { name: /Proposals/ }), { button: 0 });
    const tab = await screen.findByTestId("context-proposals-tab");
    expect(within(tab).getByTestId("diff-original").textContent).toBe("old body");
    expect(within(tab).getByTestId("diff-modified").textContent).toBe("new body");
    expect(tab.textContent).toContain("Sources: session:conv_1");
    fireEvent.click(within(tab).getByRole("button", { name: /Apply/ }));
    await waitFor(() => expect(api.applyContextProposal).toHaveBeenCalledWith("p1", "abc"));
    expect(await screen.findByTestId("context-proposals-empty")).toBeTruthy();
  });

  it("blocks applying a stale proposal", async () => {
    vi.mocked(api.fetchContextProposals).mockResolvedValue({
      proposals: [
        {
          id: "s1",
          path: "wiki/exp.md",
          action: "edit",
          new_content: "x",
          rationale: "",
          sources: [],
          base_sha: "old",
          created_at: 1,
          job_id: null,
          current_content: "y",
          stale: true,
        },
      ],
    });
    renderPage();
    fireEvent.mouseDown(await screen.findByRole("tab", { name: /Proposals/ }), { button: 0 });
    const tab = await screen.findByTestId("context-proposals-tab");
    expect(tab.textContent).toContain("file changed since proposed");
    expect(within(tab).getByRole("button", { name: /Apply/ })).toBeDisabled();
  });
});
