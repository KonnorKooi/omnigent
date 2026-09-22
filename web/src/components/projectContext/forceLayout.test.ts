import { describe, expect, it } from "vitest";
import { colorForGroup, createLayout, layoutBounds, tickLayout } from "./forceLayout";

describe("forceLayout", () => {
  it("is deterministic and spreads a graph out before cooling", () => {
    const edges = Array.from({ length: 199 }, (_, i) => [i, i + 1] as const);
    const run = () => {
      const state = createLayout(200);
      let ticks = 0;
      while (tickLayout(state, { count: 200, edges }) && ticks < 2000) ticks++;
      return { state, ticks };
    };
    const a = run();
    const b = run();
    expect(Array.from(a.state.x)).toEqual(Array.from(b.state.x));
    expect(a.ticks).toBeLessThan(2000);
    const bounds = layoutBounds(a.state);
    expect(bounds.maxX - bounds.minX).toBeGreaterThan(50);
    expect(a.state.x.every((v) => Number.isFinite(v))).toBe(true);
  });

  it("keeps connected nodes closer than unconnected ones", () => {
    const state = createLayout(3);
    for (let i = 0; i < 400; i++) tickLayout(state, { count: 3, edges: [[0, 1]] });
    const dist = (i: number, j: number) =>
      Math.hypot(state.x[i]! - state.x[j]!, state.y[i]! - state.y[j]!);
    expect(dist(0, 1)).toBeLessThan(dist(0, 2));
  });

  it("handles empty graphs and gives stable group colours", () => {
    expect(tickLayout(createLayout(0), { count: 0, edges: [] })).toBe(false);
    expect(colorForGroup(3)).toBe(colorForGroup("3"));
    expect(colorForGroup("wiki")).not.toBe(colorForGroup("system"));
  });
});
