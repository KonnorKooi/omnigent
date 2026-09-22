import { describe, expect, it } from "vitest";
import { agentGuide } from "./agentGuides";

describe("agentGuide", () => {
  it("returns the guide for a known agent", () => {
    expect(agentGuide("duo")?.when).toMatch(/targeted code changes/);
    expect(agentGuide("claude-native-ui")?.summary).toMatch(/Claude Code/);
  });

  it("returns undefined for unknown or missing names", () => {
    expect(agentGuide("not-an-agent")).toBeUndefined();
    expect(agentGuide(undefined)).toBeUndefined();
  });
});
