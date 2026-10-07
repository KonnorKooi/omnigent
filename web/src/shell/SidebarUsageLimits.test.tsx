import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SidebarUsageLimits, formatReset } from "./SidebarUsageLimits";

const fetchUsageLimits = vi.fn();
vi.mock("@/lib/usageApi", () => ({ fetchUsageLimits: () => fetchUsageLimits() }));

function renderLimits() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarUsageLimits />
    </QueryClientProvider>,
  );
}

describe("SidebarUsageLimits", () => {
  beforeEach(() => {
    fetchUsageLimits.mockReset();
    window.localStorage.clear();
  });

  it("renders nothing until a provider has reported", async () => {
    fetchUsageLimits.mockResolvedValue([]);
    const { container } = renderLimits();
    await vi.waitFor(() => expect(fetchUsageLimits).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows each provider's windows with percentages", async () => {
    fetchUsageLimits.mockResolvedValue([
      {
        provider: "claude",
        updatedAt: 0,
        windows: [
          { id: "five_hour", label: "5h", usedPercent: 42.4, resetsAt: null },
          { id: "seven_day", label: "wk", usedPercent: null, resetsAt: null },
        ],
      },
      {
        provider: "codex",
        updatedAt: 0,
        windows: [{ id: "primary", label: "5h", usedPercent: 91, resetsAt: null }],
      },
    ]);
    renderLimits();
    expect(await screen.findByTestId("sidebar-usage-limits")).toBeInTheDocument();
    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.getByText("42%")).toBeInTheDocument();
    expect(screen.getByText("91%")).toBeInTheDocument();
    expect(screen.getByText("–")).toBeInTheDocument();
  });

  it("paints the cached report before the fetch resolves", () => {
    window.localStorage.setItem(
      "omnigent.usageLimits",
      JSON.stringify([
        {
          provider: "claude",
          updatedAt: 0,
          windows: [{ id: "five_hour", label: "5h", usedPercent: 33, resetsAt: null }],
        },
      ]),
    );
    fetchUsageLimits.mockReturnValue(new Promise(() => {}));
    renderLimits();
    expect(screen.getByTestId("sidebar-usage-limits")).toBeInTheDocument();
    expect(screen.getByText("33%")).toBeInTheDocument();
  });

  it("keeps the last values when the server's cache is empty", async () => {
    fetchUsageLimits.mockResolvedValueOnce([
      {
        provider: "codex",
        updatedAt: 0,
        windows: [{ id: "primary", label: "5h", usedPercent: 12, resetsAt: null }],
      },
    ]);
    const first = renderLimits();
    expect(await screen.findByText("12%")).toBeInTheDocument();
    first.unmount();
    fetchUsageLimits.mockResolvedValue([]);
    renderLimits();
    await vi.waitFor(() => expect(fetchUsageLimits).toHaveBeenCalledTimes(2));
    expect(screen.getByText("12%")).toBeInTheDocument();
  });

  it("shows a dash for a cached window whose reset has passed", () => {
    window.localStorage.setItem(
      "omnigent.usageLimits",
      JSON.stringify([
        {
          provider: "claude",
          updatedAt: 0,
          windows: [{ id: "five_hour", label: "5h", usedPercent: 80, resetsAt: 1 }],
        },
      ]),
    );
    fetchUsageLimits.mockReturnValue(new Promise(() => {}));
    renderLimits();
    expect(screen.getByText("–")).toBeInTheDocument();
    expect(screen.queryByText("80%")).not.toBeInTheDocument();
  });

  it("formats near resets as a clock time and far ones with a weekday", () => {
    const now = Date.UTC(2026, 0, 5, 12);
    const near = formatReset(now / 1000 + 3600, now);
    const far = formatReset(now / 1000 + 3 * 86400, now);
    expect(near).not.toMatch(/Mon|Tue|Wed|Thu|Fri|Sat|Sun/);
    expect(far).toMatch(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)/);
  });
});
