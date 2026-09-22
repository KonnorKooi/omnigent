// Tests for the admin-only read-only transcript viewer
// (`/settings/governance/:sessionId`).
//
// The point of this surface is what it CANNOT do: an admin reading someone
// else's session must get no composer, no steering, and no approval buttons.
// The server hands an admin `LEVEL_OWNER` on every session, so nothing on the
// client may derive affordances from permission level — these tests pin the
// absence of every write affordance, not just the presence of the transcript.
//
// Mocks the API module seams (`getSession` / `fetchSessionItemsPageAsc` /
// `fetchGovernanceSession`) and the mode-agnostic admin gate.
// The items query uses `useInfiniteQuery`; the mock resolves the first page.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type * as ItemsToBlocksModule from "@/lib/itemsToBlocks";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GovernanceSessionPage } from "./GovernanceSessionPage";
import type { AnyBlock, BlockContext } from "@/lib/blocks";
import type { ConversationItem } from "@/lib/conversationItems";
import type { GovernanceSession } from "@/lib/governanceApi";
import type { Session } from "@/lib/types";
import * as governanceApi from "@/lib/governanceApi";
import * as sessionsApi from "@/lib/sessionsApi";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { ForkDialogContextProvider } from "@/shell/ForkDialogContext";

const mocks = vi.hoisted(() => ({
  isAdmin: true,
  adminPending: false,
  /**
   * When set, replaces the real `itemsToBlocks` output. Elicitations are not
   * (yet) persisted as conversation items, so the only way to drive a pending
   * approval through this page's pipeline is to inject the block directly.
   */
  blocksOverride: null as unknown[] | null,
}));

vi.mock("@/hooks/useIsAdmin", () => ({
  useIsAdmin: () => mocks.isAdmin,
  useIsAdminStatus: () => ({ isAdmin: mocks.isAdmin, isPending: mocks.adminPending }),
}));

vi.mock("@/lib/sessionsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof sessionsApi>();
  return { ...actual, getSession: vi.fn(), fetchSessionItemsPageAsc: vi.fn() };
});

vi.mock("@/lib/governanceApi", async (importOriginal) => {
  const actual = await importOriginal<typeof governanceApi>();
  return { ...actual, fetchGovernanceSession: vi.fn() };
});

vi.mock("@/lib/itemsToBlocks", async (importOriginal) => {
  const actual = await importOriginal<typeof ItemsToBlocksModule>();
  return {
    ...actual,
    itemsToBlocks: (items: ConversationItem[]) =>
      (mocks.blocksOverride as AnyBlock[] | null) ?? actual.itemsToBlocks(items),
  };
});

const SESSION_ID = "conv_abc123";

function ctx(overrides: Partial<BlockContext> = {}): BlockContext {
  return {
    agent: null,
    depth: 0,
    turn: 0,
    timestamp: 0,
    responseId: "resp_1",
    itemId: "item_1",
    ...overrides,
  };
}

/** A minimal `Session` snapshot — only the fields this page reads matter. */
function sessionSnapshot(overrides: Partial<Session> = {}): Session {
  return {
    id: SESSION_ID,
    title: "Refactor the auth guard",
    workspace: "/repos/omnigent",
    totalCostUsd: 0.42,
    usageByModel: null,
    items: [],
    ...overrides,
  } as Session;
}

function governanceRow(overrides: Partial<GovernanceSession> = {}): GovernanceSession {
  return {
    id: SESSION_ID,
    title: "Refactor the auth guard",
    source: "live",
    externalSessionId: null,
    owner: "alice",
    agentId: "ag_claude",
    workspace: "/repos/omnigent",
    gitBranch: "main",
    itemCount: 2,
    totalCostUsd: 0.42,
    createdAt: 1_730_000_000,
    updatedAt: 1_730_000_900,
    ...overrides,
  };
}

function userItem(text: string, id = "item_u1"): ConversationItem {
  return {
    id,
    type: "message",
    role: "user",
    status: "completed",
    response_id: "resp_1",
    content: [{ type: "input_text", text }],
  } as ConversationItem;
}

function assistantItem(text: string, id = "item_a1"): ConversationItem {
  return {
    id,
    type: "message",
    role: "assistant",
    status: "completed",
    response_id: "resp_1",
    content: [{ type: "output_text", text }],
  } as ConversationItem;
}

/** An elicitation block, injected through the `itemsToBlocks` seam. */
function elicitationBlock(overrides: Record<string, unknown> = {}): AnyBlock {
  return {
    type: "elicitation",
    ctx: ctx({ itemId: "item_e1" }),
    elicitationId: "elic_1",
    message: "Approval needed.",
    phase: "tool_call",
    policyName: "shell-guard",
    contentPreview: "",
    requestedSchema: {},
    status: "pending",
    response: null,
    ...overrides,
  } as AnyBlock;
}

/**
 * Live chat contexts, mirroring production: this page renders through
 * `AppShell`'s outlet, which sits INSIDE both providers. Tests must supply
 * them or every "no write affordance" assertion passes for the wrong reason —
 * the affordance would be absent because the context is, not because the page
 * suppressed it.
 */
const openFile = vi.fn();
const openForkDialog = vi.fn();

function liveFileViewer() {
  return {
    openFile,
    // Required by this version's context; the page must not reach it either.
    openGithubTab: () => {},
    isChangedPath: (path: string) => path === "src/app.ts",
    conversationId: "conv_some_other_session",
    workspaceRoot: "/repos/omnigent",
    workspaceHome: "/home/u",
  };
}

function renderPage(sessionId = SESSION_ID) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/settings/governance/${sessionId}`]}>
        <FileViewerContext.Provider value={liveFileViewer()}>
          <ForkDialogContextProvider value={{ canFork: true, openForkDialog }}>
            <GovernanceSessionPage sessionId={sessionId} />
          </ForkDialogContextProvider>
        </FileViewerContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.isAdmin = true;
  mocks.adminPending = false;
  mocks.blocksOverride = null;
  vi.mocked(sessionsApi.getSession).mockResolvedValue(sessionSnapshot());
  vi.mocked(sessionsApi.fetchSessionItemsPageAsc).mockResolvedValue({
    items: [userItem("Please fix the login redirect"), assistantItem("Done — patched the guard.")],
    hasMore: false,
  });
  vi.mocked(governanceApi.fetchGovernanceSession).mockResolvedValue(governanceRow());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GovernanceSessionPage transcript", () => {
  it("renders the session title and both sides of the conversation", async () => {
    renderPage();

    expect(await screen.findByText("Refactor the auth guard")).toBeInTheDocument();
    expect(await screen.findByText("Please fix the login redirect")).toBeInTheDocument();
    expect(await screen.findByText("Done — patched the guard.")).toBeInTheDocument();
  });

  it("shows the owner, workspace, and source so an auditor knows whose session this is", async () => {
    renderPage();

    // The header shell paints before the queries resolve, so anchor on a
    // value that only arrives with the data.
    await screen.findByText("alice");
    const header = screen.getByTestId("governance-session-header");
    expect(within(header).getByText("alice")).toBeInTheDocument();
    expect(within(header).getByText("/repos/omnigent")).toBeInTheDocument();
    expect(within(header).getByTestId("source-badge")).toHaveTextContent("live");
  });

  it("links back to the governance table", async () => {
    renderPage();

    expect(await screen.findByRole("link", { name: /back to governance/i })).toHaveAttribute(
      "href",
      "/settings/governance",
    );
  });

  it("reads the transcript under a governance-scoped query key", async () => {
    // Sharing `["session", id]` with the chat surface would let a cached slim
    // snapshot (no items) serve this page, which needs them.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/settings/governance/${SESSION_ID}`]}>
          <GovernanceSessionPage sessionId={SESSION_ID} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("Please fix the login redirect");

    const keys = client
      .getQueryCache()
      .getAll()
      .map((q) => q.queryKey[0]);
    expect(keys).toContain("governance-session");
    expect(keys).not.toContain("session");
  });
});

describe("GovernanceSessionPage is read-only", () => {
  it("renders no composer or send control", async () => {
    renderPage();
    await screen.findByText("Please fix the login redirect");

    // A composer is the single largest write affordance; its absence is the
    // whole point of this page existing separately from ChatPage.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /send/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /stop/i })).toBeNull();
  });

  it("renders no fork action even though the fork dialog is available", async () => {
    // `canFork: true` above is the production condition — AppShell provides a
    // live fork dialog around this page. Suppressing the action has to be the
    // page's own doing.
    renderPage();
    await screen.findByText("Done — patched the guard.");

    expect(screen.queryByRole("button", { name: /fork/i })).toBeNull();
    expect(openForkDialog).not.toHaveBeenCalled();
  });

  it("renders a workspace file path as plain code, not a link into the file viewer", async () => {
    // AppShell's FileViewerContext is live here and `isChangedPath` matches,
    // so without the page's own reset this path would linkify and open an
    // editor scoped to whatever session the viewer holds — a write surface.
    vi.mocked(sessionsApi.fetchSessionItemsPageAsc).mockResolvedValue({
      items: [assistantItem("Patched `src/app.ts` for you.")],
      hasMore: false,
    });
    renderPage();

    const span = await screen.findByText("src/app.ts");
    expect(span.tagName).toBe("CODE");
    expect(screen.queryByRole("button", { name: "src/app.ts" })).toBeNull();
    expect(openFile).not.toHaveBeenCalled();
  });

  it("renders a markdown link as inert text, keeping the target readable", async () => {
    // Transcript content is attacker-authorable. With a live anchor, a link
    // in someone else's session is a navigation channel out of a page whose
    // whole point is that it offers none — and the server hands an admin
    // LEVEL_OWNER, so wherever they land works. The URL still has to be
    // legible: an auditor must see what was linked.
    vi.mocked(sessionsApi.fetchSessionItemsPageAsc).mockResolvedValue({
      items: [assistantItem("See [click me](https://evil.example/x) for details.")],
      hasMore: false,
    });
    renderPage();

    await screen.findByText(/click me/);
    // Scoped to the bubble: the page header keeps its own "Back to
    // governance" link, which is the page's own navigation, not content's.
    const bubble = screen.getByTestId("message-bubble");
    expect(within(bubble).queryByRole("link")).toBeNull();
    expect(document.querySelector('a[href*="evil.example"]')).toBeNull();
    // The target is still shown, just not followable.
    expect(within(bubble).getByText(/https:\/\/evil\.example\/x/)).toBeInTheDocument();
  });

  it("renders a pending elicitation inert — visible, but with no approve or reject button", async () => {
    // A historical pending approval must never render live buttons: without a
    // submitter, `ApprovalCard` falls back to the chat store and would POST a
    // verdict against whatever session that store currently holds.
    mocks.blocksOverride = [
      {
        type: "elicitation",
        ctx: ctx({ itemId: "item_e1" }),
        elicitationId: "elic_1",
        message: "Allow running `rm -rf build/`?",
        phase: "tool_call",
        policyName: "shell-guard",
        contentPreview: "rm -rf build/",
        requestedSchema: {},
        status: "pending",
        response: null,
      },
    ] satisfies AnyBlock[];
    renderPage();

    // The auditor still sees that an approval was requested…
    expect(await screen.findByText(/Allow running/)).toBeInTheDocument();
    // …but cannot act on it.
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /decline/i })).toBeNull();
  });
});

describe("GovernanceSessionPage elicitation audit record", () => {
  // "An approval happened" is not the audit answer — "what was approved" is.
  // These pin that the structured payload survives into the record, since the
  // raw contentPreview is suppressed for exactly these kinds.

  it("shows the command and cwd for a Codex command approval", async () => {
    mocks.blocksOverride = [
      elicitationBlock({
        contentPreview: '{"tool_use_id":"toolu_01internal","cmd":["rm"]}',
        codexCommand: {
          command: "rm -rf build/",
          cwd: "/repos/omnigent",
          reason: "Clearing stale build output",
          execPolicyAmendment: null,
        },
        status: "responded",
        response: { action: "accept" },
      }),
    ];
    renderPage();

    const record = await screen.findByTestId("governance-elicitation-command");
    expect(within(record).getByText("rm -rf build/")).toBeInTheDocument();
    expect(within(record).getByText("/repos/omnigent")).toBeInTheDocument();
    expect(within(record).getByText("Clearing stale build output")).toBeInTheDocument();
    // The transport JSON carries internal ids and is not the request.
    expect(screen.queryByText(/toolu_01internal/)).toBeNull();
  });

  it("shows the plan text for an ExitPlanMode approval", async () => {
    mocks.blocksOverride = [
      elicitationBlock({
        contentPreview: '{"tool_use_id":"toolu_01internal"}',
        exitPlanMode: { plan: "Step one: rewrite the auth guard." },
        status: "responded",
        response: { action: "accept" },
      }),
    ];
    renderPage();

    const plan = await screen.findByTestId("governance-elicitation-plan");
    expect(plan).toHaveTextContent("Step one: rewrite the auth guard.");
    expect(screen.queryByText(/toolu_01internal/)).toBeNull();
  });

  it("shows the questions, options, and chosen answer for an AskUserQuestion prompt", async () => {
    mocks.blocksOverride = [
      elicitationBlock({
        contentPreview: '{"tool_use_id":"toolu_01internal"}',
        askUserQuestion: {
          questions: [
            {
              question: "Which database?",
              header: "Storage",
              multiSelect: false,
              options: [{ label: "Postgres", description: "Relational" }, { label: "SQLite" }],
            },
          ],
        },
        status: "responded",
        response: { action: "accept", content: { "Which database?": "Postgres" } },
      }),
    ];
    renderPage();

    const questions = await screen.findByTestId("governance-elicitation-questions");
    expect(within(questions).getByText(/Which database\?/)).toBeInTheDocument();
    expect(within(questions).getByText(/Postgres — Relational/)).toBeInTheDocument();
    expect(within(questions).getByText("SQLite")).toBeInTheDocument();

    // And what was actually chosen — the point of the audit.
    const answers = screen.getByTestId("governance-elicitation-answers");
    expect(answers).toHaveTextContent("Which database?: Postgres");
  });

  it("renders an external approval URL as plain text, never as a navigable link", async () => {
    // The record must name where the approval was handled without offering a
    // way into a live approval page for someone else's session.
    mocks.blocksOverride = [elicitationBlock({ url: "https://vendor.example.com/approve/xyz" })];
    renderPage();

    const url = await screen.findByTestId("governance-elicitation-url");
    expect(url).toHaveTextContent("https://vendor.example.com/approve/xyz");
    expect(screen.queryByRole("link", { name: /vendor\.example\.com/ })).toBeNull();
    expect(document.querySelector('a[href*="vendor.example.com"]')).toBeNull();
  });
});

describe("GovernanceSessionPage cost", () => {
  it("renders the session cost when it was priced", async () => {
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("governance-session-cost")).toHaveTextContent("$0.42"),
    );
  });

  it("renders an em dash — never $0.00 — when the session was never priced", async () => {
    // `null` means "never priced", which is a different answer from "cost
    // nothing". Rendering $0.00 there asserts a zero the server never computed.
    //
    // The cost cell also reads "—" before the snapshot lands, so the usage
    // breakdown — which comes ONLY from `getSession` — anchors the assertion
    // to a resolved query rather than the pre-fetch paint.
    vi.mocked(sessionsApi.getSession).mockResolvedValue(
      sessionSnapshot({
        totalCostUsd: null,
        usageByModel: {
          "claude-opus-4": {
            inputTokens: 100,
            outputTokens: 50,
            totalTokens: 150,
            cacheReadInputTokens: null,
            cacheCreationInputTokens: null,
            totalCostUsd: null,
          },
        },
      }),
    );
    vi.mocked(governanceApi.fetchGovernanceSession).mockResolvedValue(
      governanceRow({ totalCostUsd: null }),
    );
    renderPage();

    await screen.findByTestId("agent-info-usage-by-model");
    const cost = screen.getByTestId("governance-session-cost");
    expect(cost).toHaveTextContent("—");
    expect(cost).not.toHaveTextContent("$0.00");
  });
});

describe("GovernanceSessionPage audit gating", () => {
  it("renders no transcript when the audited governance read failed", async () => {
    // `fetchGovernanceSession` is the only call that writes the access audit
    // row; `GET /v1/sessions/{id}/items` is not audited. The three queries are
    // independent and unordered, so without an explicit gate a failed
    // governance read still leaves the items query painting a full transcript
    // of another member's session with nothing recorded in the log.
    vi.mocked(governanceApi.fetchGovernanceSession).mockRejectedValue(new Error("404 Not Found"));
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load this session/);
    // The items query resolved fine — the transcript is withheld anyway.
    await waitFor(() => expect(sessionsApi.fetchSessionItemsPageAsc).toHaveBeenCalled());
    expect(screen.queryByTestId("message-bubble")).toBeNull();
    expect(screen.queryByText("Please fix the login redirect")).toBeNull();
    expect(screen.queryByText("Done — patched the guard.")).toBeNull();
  });

  it("still renders the transcript when only the plain snapshot failed", async () => {
    // `getSession` is unaudited detail (cost / usage). Its failure must not
    // withhold a transcript whose access WAS recorded — the gate is on the
    // audit write, not on every query succeeding.
    vi.mocked(sessionsApi.getSession).mockRejectedValue(new Error("boom"));
    renderPage();

    expect(await screen.findByText("Please fix the login redirect")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/Could not load this session/);
  });
});

describe("GovernanceSessionPage gating", () => {
  it("blocks non-admins with a permission message and fetches nothing", async () => {
    mocks.isAdmin = false;
    renderPage();

    expect(
      await screen.findByText("You don't have permission to view governance data."),
    ).toBeInTheDocument();
    expect(sessionsApi.getSession).not.toHaveBeenCalled();
    expect(sessionsApi.fetchSessionItemsPageAsc).not.toHaveBeenCalled();
    expect(governanceApi.fetchGovernanceSession).not.toHaveBeenCalled();
  });

  it("waits on the identity probe instead of accusing an unresolved admin", async () => {
    // `isAdmin` reads false for everyone until `/v1/me` answers; denying now
    // would tell a real admin who deep-linked here that they lack permission.
    mocks.isAdmin = false;
    mocks.adminPending = true;
    renderPage();

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("You don't have permission to view governance data.")).toBeNull();
    await waitFor(() => expect(sessionsApi.getSession).not.toHaveBeenCalled());
  });
});
