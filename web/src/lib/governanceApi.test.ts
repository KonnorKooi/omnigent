// Unit tests for `governanceApi.ts` — the snake_case wire contract of
// `GET /v1/governance/sessions`.
//
// GovernancePage.test.tsx mocks this module, so the page suite proves nothing
// about the wire itself. These tests own that boundary: the query-param names
// the server expects, the wire→camelCase conversion, and the 403 a non-admin
// gets surfacing as a thrown ApiError (react-query needs a throw to populate
// `isError`).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as identity from "./identity";
import { ApiError } from "./sessionsApi";
import { fetchGovernanceSessions } from "./governanceApi";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: init?.ok === false ? "Forbidden" : "OK",
    json: async () => body,
  } as unknown as Response;
}

const emptyPage = { data: [], first_id: null, last_id: null, has_more: false };

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(mockResponse(emptyPage));
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** The URL the last `authenticatedFetch` call was made against. */
function requestedUrl(): string {
  return fetchSpy.mock.calls[0]![0] as string;
}

describe("fetchGovernanceSessions request", () => {
  it("GETs the bare route when no filters are set", async () => {
    await fetchGovernanceSessions();
    expect(requestedUrl()).toBe("/v1/governance/sessions");
  });

  it("maps camelCase filters onto the server's snake_case query params", async () => {
    await fetchGovernanceSessions({
      workspacePrefix: "/repos/omnigent",
      agentId: "ag_claude",
      updatedAfter: 1_730_000_000,
      updatedBefore: 1_730_009_999,
      searchQuery: "auth guard",
      limit: 25,
      after: "conv_cursor",
      order: "desc",
      sortBy: "updated_at",
    });
    const params = new URLSearchParams(requestedUrl().split("?")[1]);
    expect(params.get("workspace_prefix")).toBe("/repos/omnigent");
    expect(params.get("agent_id")).toBe("ag_claude");
    expect(params.get("updated_after")).toBe("1730000000");
    expect(params.get("updated_before")).toBe("1730009999");
    expect(params.get("search_query")).toBe("auth guard");
    expect(params.get("limit")).toBe("25");
    expect(params.get("after")).toBe("conv_cursor");
    expect(params.get("order")).toBe("desc");
    expect(params.get("sort_by")).toBe("updated_at");
  });

  it("repeats `source` and `owner` once per value rather than joining them", async () => {
    // The route reads these as repeatable params (OR-ed server-side); a
    // comma-joined single param would be read as one literal value.
    await fetchGovernanceSessions({
      source: ["live", "import:claude"],
      owner: ["alice", "bob"],
    });
    const params = new URLSearchParams(requestedUrl().split("?")[1]);
    expect(params.getAll("source")).toEqual(["live", "import:claude"]);
    expect(params.getAll("owner")).toEqual(["alice", "bob"]);
  });

  it("omits empty filters instead of sending blank params", async () => {
    await fetchGovernanceSessions({ source: [], owner: [], workspacePrefix: "", searchQuery: "" });
    expect(requestedUrl()).toBe("/v1/governance/sessions");
  });
});

describe("fetchGovernanceSessions response", () => {
  it("converts the wire row to camelCase and preserves the page cursors", async () => {
    fetchSpy.mockResolvedValue(
      mockResponse({
        data: [
          {
            id: "conv_abc",
            title: "Refactor the auth guard",
            source: "import:claude",
            external_session_id: "ext_123",
            owner: "alice",
            agent_id: "ag_claude",
            workspace: "/repos/omnigent",
            git_branch: "feature/auth",
            item_count: 12,
            total_cost_usd: 0.42,
            created_at: 1_730_000_000,
            updated_at: 1_730_000_900,
          },
        ],
        first_id: "conv_abc",
        last_id: "conv_abc",
        has_more: true,
      }),
    );

    const page = await fetchGovernanceSessions();
    expect(page.data[0]).toEqual({
      id: "conv_abc",
      title: "Refactor the auth guard",
      source: "import:claude",
      externalSessionId: "ext_123",
      owner: "alice",
      agentId: "ag_claude",
      workspace: "/repos/omnigent",
      gitBranch: "feature/auth",
      itemCount: 12,
      totalCostUsd: 0.42,
      createdAt: 1_730_000_000,
      updatedAt: 1_730_000_900,
    });
    expect(page.firstId).toBe("conv_abc");
    expect(page.lastId).toBe("conv_abc");
    expect(page.hasMore).toBe(true);
  });

  it("keeps an unpriced session's cost as null rather than coercing it to 0", async () => {
    // `null` means "never priced" — collapsing it to 0 would let the table
    // render a $0.00 the server never computed.
    fetchSpy.mockResolvedValue(
      mockResponse({
        data: [
          {
            id: "conv_abc",
            source: "live",
            item_count: 0,
            total_cost_usd: null,
            created_at: 1,
            updated_at: 2,
          },
        ],
        first_id: null,
        last_id: null,
        has_more: false,
      }),
    );

    const page = await fetchGovernanceSessions();
    expect(page.data[0]!.totalCostUsd).toBeNull();
    // Fields the server omits entirely normalize to null, not undefined.
    expect(page.data[0]!.title).toBeNull();
    expect(page.data[0]!.owner).toBeNull();
    expect(page.data[0]!.workspace).toBeNull();
  });
});

describe("fetchGovernanceSessions errors", () => {
  it("throws an ApiError carrying the server's code when a non-admin is refused", async () => {
    fetchSpy.mockResolvedValue(
      mockResponse(
        // Verbatim from the route's `_require_admin` gate.
        {
          error: {
            code: "forbidden",
            message: "Admin privileges required to view governance data",
          },
        },
        { ok: false, status: 403 },
      ),
    );

    await expect(fetchGovernanceSessions()).rejects.toThrow(ApiError);
    await expect(fetchGovernanceSessions()).rejects.toMatchObject({
      status: 403,
      code: "forbidden",
      message: "Admin privileges required to view governance data",
    });
  });

  it("falls back to the status line when the body is not the structured error shape", async () => {
    // A non-JSON body must still surface a usable message, not crash the parse.
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);

    await expect(fetchGovernanceSessions()).rejects.toMatchObject({
      status: 500,
      code: null,
      message: "500 Internal Server Error",
    });
  });
});
