// Tests for the admin-only GovernancePage (every session in the workspace).
//
// Browser e2e is impractical (admin-gated, and the list spans other users'
// sessions), so the surface is pinned here by mocking the API module seam
// (`fetchGovernanceSessions`) and the mode-agnostic admin gate
// (`useIsAdminStatus`). The server route is exercised separately; this suite
// owns the rendering, filtering, and gating contract.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GovernancePage } from "./GovernancePage";
import type { GovernanceSession, GovernanceSessionsPage } from "@/lib/governanceApi";
import * as governanceApi from "@/lib/governanceApi";

const mocks = vi.hoisted(() => ({ isAdmin: true, adminPending: false }));

vi.mock("@/hooks/useIsAdmin", () => ({
  useIsAdmin: () => mocks.isAdmin,
  useIsAdminStatus: () => ({ isAdmin: mocks.isAdmin, isPending: mocks.adminPending }),
}));
vi.mock("@/lib/governanceApi", () => ({
  fetchGovernanceSessions: vi.fn(),
  GOVERNANCE_PAGE_SIZE: 25,
}));

function session(overrides: Partial<GovernanceSession> = {}): GovernanceSession {
  return {
    id: "conv_abc123",
    title: "Refactor the auth guard",
    source: "live",
    externalSessionId: null,
    owner: "alice",
    agentId: "ag_claude",
    workspace: "/repos/omnigent",
    gitBranch: "main",
    itemCount: 12,
    totalCostUsd: 0.42,
    createdAt: 1_730_000_000,
    updatedAt: 1_730_000_900,
    ...overrides,
  };
}

function page(data: GovernanceSession[], overrides: Partial<GovernanceSessionsPage> = {}) {
  return {
    data,
    firstId: data[0]?.id ?? null,
    lastId: data[data.length - 1]?.id ?? null,
    hasMore: false,
    ...overrides,
  };
}

/** Mirrors the page's own debounce window; tests wait past it. */
const FILTER_DEBOUNCE_MS = 300;

/** Exposes the live URL query string so filter tests can assert on it. */
function LocationProbe() {
  return <span data-testid="location-search">{useLocation().search}</span>;
}

function renderPage(initialEntry = "/settings/governance") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/settings/governance"
            element={
              <>
                <GovernancePage />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.isAdmin = true;
  mocks.adminPending = false;
  vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(page([session()]));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GovernancePage gating", () => {
  it("blocks non-admins with a permission message and never fetches sessions", async () => {
    mocks.isAdmin = false;
    renderPage();
    expect(
      await screen.findByText("You don't have permission to view governance data."),
    ).toBeInTheDocument();
    expect(governanceApi.fetchGovernanceSessions).not.toHaveBeenCalled();
  });

  it("waits on the identity probe instead of accusing an unresolved admin", async () => {
    // Before `/v1/me` answers, the gate reads `false` for everyone. Rendering
    // the denial then tells a real admin who deep-linked here that they lack
    // permission, and flips to the table a moment later.
    mocks.isAdmin = false;
    mocks.adminPending = true;
    renderPage();

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("You don't have permission to view governance data.")).toBeNull();
    expect(governanceApi.fetchGovernanceSessions).not.toHaveBeenCalled();
  });
});

describe("GovernancePage table", () => {
  it("renders a row per session with its title, owner, and workspace", async () => {
    vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(
      page([
        session({ id: "conv_1", title: "Refactor the auth guard", owner: "alice" }),
        session({
          id: "conv_2",
          title: "Fix the flaky import",
          owner: "bob",
          workspace: "/repos/other",
        }),
      ]),
    );
    renderPage();

    const firstRow = (await screen.findByText("Refactor the auth guard")).closest("tr")!;
    expect(within(firstRow).getByText("alice")).toBeInTheDocument();
    expect(within(firstRow).getByText("/repos/omnigent")).toBeInTheDocument();

    const secondRow = screen.getByText("Fix the flaky import").closest("tr")!;
    expect(within(secondRow).getByText("bob")).toBeInTheDocument();
    expect(within(secondRow).getByText("/repos/other")).toBeInTheDocument();
  });

  it("badges an imported session as 'claude import' and a live one as 'live'", async () => {
    vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(
      page([
        session({ id: "conv_1", title: "Imported work", source: "import:claude" }),
        session({ id: "conv_2", title: "Live work", source: "live" }),
      ]),
    );
    renderPage();

    const importedRow = (await screen.findByText("Imported work")).closest("tr")!;
    expect(within(importedRow).getByText("claude import")).toBeInTheDocument();

    const liveRow = screen.getByText("Live work").closest("tr")!;
    expect(within(liveRow).getByText("live")).toBeInTheDocument();
  });

  it("distinguishes a never-priced session from one that genuinely cost nothing", async () => {
    // `total_cost_usd: null` means "never priced" and must read as an em dash;
    // a real 0 must read as $0.00. Collapsing either into the other loses the
    // distinction in both directions, so both are pinned here.
    vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(
      page([
        session({ id: "conv_1", title: "Unpriced", totalCostUsd: null }),
        session({ id: "conv_2", title: "Free", totalCostUsd: 0 }),
        session({ id: "conv_3", title: "Priced", totalCostUsd: 0.42 }),
      ]),
    );
    renderPage();

    // Assert on the Cost cell specifically (the last column) — other columns
    // also render an em dash for their own missing values.
    const costCell = (row: HTMLElement) => within(row).getAllByRole("cell").at(-1)!;

    const unpricedRow = (await screen.findByText("Unpriced")).closest("tr")!;
    expect(costCell(unpricedRow)).toHaveTextContent("—");
    expect(costCell(unpricedRow)).not.toHaveTextContent("$0.00");

    const freeRow = screen.getByText("Free").closest("tr")!;
    expect(costCell(freeRow)).toHaveTextContent("$0.00");

    const pricedRow = screen.getByText("Priced").closest("tr")!;
    expect(costCell(pricedRow)).toHaveTextContent("$0.42");
  });

  it("links each row to its governance transcript", async () => {
    vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(
      page([session({ id: "conv_xyz", title: "Refactor the auth guard" })]),
    );
    renderPage();

    expect(await screen.findByRole("link", { name: "Refactor the auth guard" })).toHaveAttribute(
      "href",
      "/settings/governance/conv_xyz",
    );
  });
});

describe("GovernancePage empty state", () => {
  it("stays silent while the table is still auto-paging", async () => {
    // A first page fully consumed by rows the caller filtered out comes back
    // empty with hasMore=true, and the sentinel immediately fetches the next
    // one. Transient, but a false "no sessions match" is the one message an
    // audit surface must not show.
    vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(
      page([], { hasMore: true, lastId: "conv_cursor" }),
    );
    renderPage();

    await waitFor(() => expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalled());
    // The sentinel is up (more pages to come), so the verdict is not in yet.
    expect(await screen.findByRole("button", { name: /load more/i })).toBeInTheDocument();
    expect(screen.queryByText("No sessions match these filters.")).toBeNull();
  });

  it("says so once paging is exhausted and nothing matched", async () => {
    vi.mocked(governanceApi.fetchGovernanceSessions).mockResolvedValue(page([]));
    renderPage();

    expect(await screen.findByText("No sessions match these filters.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /load more/i })).toBeNull();
  });
});

describe("GovernancePage filters", () => {
  it("writes the workspace filter to the URL and refetches with workspace_prefix", async () => {
    renderPage();
    await screen.findByText("Refactor the auth guard");

    fireEvent.change(screen.getByLabelText("Workspace"), { target: { value: "/repos/omnigent" } });

    // Shareable: the filter round-trips through the URL, not local state.
    await waitFor(() =>
      expect(screen.getByTestId("location-search").textContent).toContain(
        "workspace=%2Frepos%2Fomnigent",
      ),
    );
    await waitFor(() =>
      expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
        expect.objectContaining({ workspacePrefix: "/repos/omnigent" }),
      ),
    );
  });

  it("seeds the filter inputs from the URL so a shared link reproduces the view", async () => {
    renderPage("/settings/governance?workspace=%2Frepos%2Fother&source=import%3Aclaude");
    await screen.findByText("Refactor the auth guard");

    expect(screen.getByLabelText("Workspace")).toHaveValue("/repos/other");
    expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
      expect.objectContaining({ workspacePrefix: "/repos/other", source: ["import:claude"] }),
    );
  });

  it("debounces typing into one request instead of one per keystroke", async () => {
    renderPage();
    await screen.findByText("Refactor the auth guard");
    vi.mocked(governanceApi.fetchGovernanceSessions).mockClear();

    // Each distinct filter value is its own query key against a list spanning
    // every session on the server, so per-keystroke commits would fan a typed
    // path out into one server-wide query per character.
    const input = screen.getByLabelText("Workspace");
    for (const value of ["/", "/r", "/re", "/rep", "/repo", "/repos"]) {
      fireEvent.change(input, { target: { value } });
    }

    await waitFor(() =>
      expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
        expect.objectContaining({ workspacePrefix: "/repos" }),
      ),
    );
    expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledTimes(1);
  });

  it("keeps the table on screen while a filter change is in flight", async () => {
    // A new filter is a new cache key with no data of its own. Without
    // keepPreviousData the table unmounts and the page blanks to "Loading…"
    // under the user's cursor on every change.
    renderPage();
    await screen.findByText("Refactor the auth guard");

    let release: (value: GovernanceSessionsPage) => void = () => {};
    vi.mocked(governanceApi.fetchGovernanceSessions).mockReturnValue(
      new Promise<GovernanceSessionsPage>((resolve) => {
        release = resolve;
      }),
    );

    fireEvent.change(screen.getByLabelText("Workspace"), { target: { value: "/repos/omnigent" } });
    await waitFor(() => expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledTimes(2));

    // Still showing the previous page, not a blank loading state.
    expect(screen.getByText("Refactor the auth guard")).toBeInTheDocument();

    release(page([session({ id: "conv_new", title: "Filtered result" })]));
    expect(await screen.findByText("Filtered result")).toBeInTheDocument();
  });

  it("preserves every owner value in the URL rather than dropping all but the first", async () => {
    // `owner` is repeatable on the wire. Rendering only `owner[0]` would hide
    // the rest and delete them on the next edit.
    renderPage("/settings/governance?owner=alice&owner=bob");
    await screen.findByText("Refactor the auth guard");

    expect(screen.getByLabelText("Owner")).toHaveValue("alice, bob");
    expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
      expect.objectContaining({ owner: ["alice", "bob"] }),
    );

    fireEvent.change(screen.getByLabelText("Owner"), { target: { value: "alice, bob, carol" } });
    await waitFor(() =>
      expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
        expect.objectContaining({ owner: ["alice", "bob", "carol"] }),
      ),
    );
    expect(screen.getByTestId("location-search").textContent).toBe(
      "?owner=alice&owner=bob&owner=carol",
    );
  });

  it("keeps the separator while a second owner is being typed", async () => {
    // The commit normalizes "alice," to ["alice"], which re-derives as
    // "alice". Re-syncing the box on that echo deletes the comma the user just
    // typed, so a second owner is effectively unenterable.
    renderPage();
    await screen.findByText("Refactor the auth guard");
    const input = screen.getByLabelText("Owner") as HTMLInputElement;

    // First owner plus its separator, then a pause past the debounce.
    fireEvent.change(input, { target: { value: "alice," } });
    await waitFor(() =>
      expect(screen.getByTestId("location-search").textContent).toBe("?owner=alice"),
    );
    expect(input).toHaveValue("alice,");

    // Keep typing from whatever the box now holds, as a real user would.
    fireEvent.change(input, { target: { value: `${input.value} bob` } });
    await waitFor(() =>
      expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
        expect.objectContaining({ owner: ["alice", "bob"] }),
      ),
    );
    expect(screen.getByTestId("location-search").textContent).toBe("?owner=alice&owner=bob");
    expect(input).toHaveValue("alice, bob");
  });

  it("re-syncs the owner box when the URL changes from outside", async () => {
    // The self-commit guard must not swallow a genuine external change
    // (back/forward, a shared link, a reset) — that's what `value` is for.
    // Driven through the router rather than window.history, which MemoryRouter
    // does not observe.
    function ResetButton() {
      const navigate = useNavigate();
      return (
        <button type="button" onClick={() => navigate("/settings/governance?owner=carol")}>
          external-nav
        </button>
      );
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/settings/governance?owner=alice"]}>
          <Routes>
            <Route
              path="/settings/governance"
              element={
                <>
                  <GovernancePage />
                  <LocationProbe />
                  <ResetButton />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("Refactor the auth guard");
    const input = screen.getByLabelText("Owner") as HTMLInputElement;
    expect(input).toHaveValue("alice");

    // Commit once so the guard is armed with this box's own last value.
    fireEvent.change(input, { target: { value: "alice, bob" } });
    await waitFor(() =>
      expect(screen.getByTestId("location-search").textContent).toBe("?owner=alice&owner=bob"),
    );

    fireEvent.click(screen.getByText("external-nav"));
    await waitFor(() => expect(input).toHaveValue("carol"));
  });

  it("lets a navigation back to this box's own last committed value win", async () => {
    // The self-commit guard was armed on commit and never disarmed, so a
    // navigation BACK to that same value looked like the box's own echo: the
    // resync was skipped, the stale draft survived, and the debounce then
    // committed it — rewriting the URL and undoing the navigation. The test
    // above navigates to a value this box never committed, so it cannot see
    // this. Latent today only because nothing in-page navigates with search
    // params; a "Clear filters" button or a saved-view link would trip it.
    function NavTo({ search, children }: { search: string; children: string }) {
      const navigate = useNavigate();
      return (
        <button type="button" onClick={() => navigate(`/settings/governance${search}`)}>
          {children}
        </button>
      );
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/settings/governance"]}>
          <Routes>
            <Route
              path="/settings/governance"
              element={
                <>
                  <GovernancePage />
                  <LocationProbe />
                  <NavTo search="?owner=alice">nav-alice</NavTo>
                  <NavTo search="?owner=carol">nav-carol</NavTo>
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("Refactor the auth guard");
    const input = screen.getByLabelText("Owner") as HTMLInputElement;

    // 1. The box commits "carol" itself, arming the guard.
    fireEvent.change(input, { target: { value: "carol" } });
    await waitFor(() =>
      expect(screen.getByTestId("location-search").textContent).toBe("?owner=carol"),
    );

    // 2. Navigate away to something else; the draft follows.
    fireEvent.click(screen.getByText("nav-alice"));
    await waitFor(() => expect(input).toHaveValue("alice"));

    // 3. Navigate back to the box's own earlier value. It must win.
    fireEvent.click(screen.getByText("nav-carol"));
    await waitFor(() => expect(input).toHaveValue("carol"));

    // ...and keep winning: a surviving "alice" draft would be committed once
    // the debounce elapses, silently reverting the navigation.
    await new Promise((resolve) => {
      setTimeout(resolve, 3 * FILTER_DEBOUNCE_MS);
    });
    expect(screen.getByTestId("location-search").textContent).toBe("?owner=carol");
    expect(input).toHaveValue("carol");
  });

  it("shows a multi-valued source filter as such instead of silently hiding values", async () => {
    // The select cannot express two sources; showing just the first would make
    // the next pick look harmless while it discarded the other.
    renderPage("/settings/governance?source=live&source=import%3Acodex");
    await screen.findByText("Refactor the auth guard");

    expect(screen.getByLabelText("Source")).toHaveDisplayValue("Multiple (2)");
    expect(governanceApi.fetchGovernanceSessions).toHaveBeenCalledWith(
      expect.objectContaining({ source: ["live", "import:codex"] }),
    );
  });
});
