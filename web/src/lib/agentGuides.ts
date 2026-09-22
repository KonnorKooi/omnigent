/**
 * Short "what / when / how" usage guides for new-session picker agents, shown
 * at the top of each agent's Edit flyout. Keyed by agent name (the server slug,
 * e.g. ``claude-native-ui``); agents without an entry show no guide.
 */
export interface AgentGuide {
  /** One-line description of what the agent is. */
  summary: string;
  /** When to pick this agent over the others. */
  when: string;
  /** How to get good results from it. */
  how: string;
}

export const AGENT_GUIDES: Record<string, AgentGuide> = {
  duo: {
    summary: "Claude writes the change, then an independent Antigravity reviewer checks the diff.",
    when: "Small, targeted code changes you want double-checked before you commit.",
    how: "Name the file and the outcome. Duo writes a contract, runs tests, gets agy's review, and reports accepted/rejected findings.",
  },
  polly: {
    summary: "Orchestrator that plans, splits work across coding sub-agents, and cross-reviews.",
    when: "Bigger multi-part tasks worth decomposing into several PRs.",
    how: "Describe the goal and constraints. Approve the plan, then watch sub-agents in the Subagents panel. Each opens its own PR.",
  },
  debby: {
    summary: "Asks Claude and GPT the same question and shows both answers side by side.",
    when: "Brainstorming, design choices, or getting a second opinion. Not for editing code.",
    how: "Ask an open question. Load the debate skill to have them critique each other.",
  },
  "claude-native-ui": {
    summary: "Claude Code in its own terminal, mirrored to the web UI.",
    when: "Default for everyday coding: multi-file edits, debugging, refactors.",
    how: "Chat normally or open the terminal to take over. Set model and permission mode here.",
  },
  "antigravity-native-ui": {
    summary: "Google Antigravity (agy, Gemini) CLI in its own terminal.",
    when: "Large-context exploration, UI/visual questions, or a second-vendor opinion on Claude's work.",
    how: "Give scoped tasks and verify claims with tests. It can report fixes that didn't land.",
  },
  "codex-native-ui": {
    summary: "OpenAI Codex CLI in its own terminal.",
    when: "Coding with GPT models, or a cross-vendor review of Claude's diff.",
    how: "Pick model and reasoning effort here. Higher effort for tricky logic.",
  },
  "cursor-native-ui": {
    summary: "Cursor's agent CLI in its own terminal.",
    when: "You want Cursor's models or agent behaviour on this repo.",
    how: "Choose an exec mode here, then chat or take over the terminal.",
  },
  "pi-native-ui": {
    summary: "Pi CLI. Can run any model your gateway exposes.",
    when: "Trying a specific model, or read-mostly review and exploration.",
    how: "Search and pick a model here, then give it a focused question.",
  },
  "opencode-native-ui": {
    summary: "OpenCode CLI in its own terminal.",
    when: "You prefer OpenCode's workflow or its provider setup.",
    how: "Chat normally or take over the terminal.",
  },
  "hermes-native-ui": {
    summary: "Hermes Agent CLI in its own terminal.",
    when: "You use Hermes and want its sessions tracked in Omnigent.",
    how: "Chat normally or take over the terminal.",
  },
  "goose-native-ui": {
    summary: "Goose CLI in its own terminal.",
    when: "You use Goose and its extensions.",
    how: "Chat normally or take over the terminal.",
  },
  "kiro-native-ui": {
    summary: "Kiro CLI in its own terminal.",
    when: "You use Kiro's spec-driven workflow.",
    how: "Chat normally or take over the terminal.",
  },
  "kimi-native-ui": {
    summary: "Kimi Code CLI in its own terminal.",
    when: "You want Moonshot's Kimi models for coding.",
    how: "Chat normally or take over the terminal.",
  },
  "qwen-native-ui": {
    summary: "Qwen Code CLI in its own terminal.",
    when: "You want Alibaba's Qwen models for coding.",
    how: "Chat normally or take over the terminal.",
  },
  grok: {
    summary: "Grok CLI, connected over ACP.",
    when: "You want xAI's Grok models on this repo.",
    how: "Chat normally. It needs its CLI installed and logged in on the host.",
  },
  jcode: {
    summary: "jcode CLI, connected over ACP.",
    when: "You use jcode and want its sessions in Omnigent.",
    how: "Chat normally. It needs its CLI installed on the host.",
  },
  devin: {
    summary: "Devin, connected over ACP.",
    when: "Delegating a task to Devin from Omnigent.",
    how: "Chat normally. It needs Devin's CLI configured on the host.",
  },
};

/**
 * Look up the usage guide for an agent.
 *
 * @param name - Agent name, e.g. ``"duo"`` or ``"claude-native-ui"``.
 * @returns The guide, or ``undefined`` when none is written for it.
 */
export function agentGuide(name: string | undefined): AgentGuide | undefined {
  return name ? AGENT_GUIDES[name] : undefined;
}
