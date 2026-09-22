import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectContextApi";
import type { SessionContextTrace } from "@/lib/projectContextApi";
import { ApiError } from "@/lib/sessionsApi";
import { SessionContextPanel } from "./SessionContextPanel";

vi.mock("@/lib/projectContextApi", () => ({ fetchSessionContextTrace: vi.fn() }));

const TRACE: SessionContextTrace = {
  project_id: "p1",
  project_name: "Timescale",
  injected: {
    text: "# Project context\nUse uv.",
    chars: 25,
    approx_tokens: 7,
    truncated: false,
    sha256: "current",
    files: ["system/rules.md"],
  },
  events: [
    { ts: 1_700_000_000, kind: "injected", sha256: "older", chars: 20 },
    {
      ts: 1_700_000_010,
      kind: "tool",
      tool: "context_read",
      args: { path: "wiki/exp.md" },
      paths: ["wiki/exp.md"],
    },
    {
      ts: 1_700_000_020,
      kind: "tool",
      tool: "graph_neighbors",
      args: { node: "Encoder" },
      node_ids: ["model_encoder", "train_main"],
    },
  ],
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContextPanel conversationId="conv_1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SessionContextPanel", () => {
  it("shows the injected text on demand and the tool call timeline", async () => {
    vi.mocked(api.fetchSessionContextTrace).mockResolvedValue(TRACE);
    renderPanel();
    const panel = await screen.findByTestId("session-context-panel");
    expect(api.fetchSessionContextTrace).toHaveBeenCalledWith("conv_1");
    expect(panel.textContent).toContain("Context tool calls (2)");
    expect(panel.textContent).toContain("context_read");
    expect(panel.textContent).toContain("model_encoder, train_main");
    expect(panel.textContent).toContain("older version");
    expect(screen.getByRole("link", { name: /Open context/ }).getAttribute("href")).toBe(
      "/projects/p1/context",
    );

    expect(panel.textContent).not.toContain("Use uv.");
    fireEvent.click(screen.getByRole("button", { name: /Injected at startup/ }));
    expect(panel.textContent).toContain("Use uv.");
  });

  it("treats a 404 as no project context", async () => {
    vi.mocked(api.fetchSessionContextTrace).mockRejectedValue(
      new ApiError("Session has no project context", 404, "not_found"),
    );
    renderPanel();
    expect(await screen.findByText(/not in a project with context/)).toBeTruthy();
  });
});
