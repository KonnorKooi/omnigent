import { describe, expect, it } from "vitest";
import type { ContextFileEntry } from "@/lib/projectContextApi";
import {
  WIKILINK_SCHEME,
  buildContextTree,
  defaultContextFile,
  parseNodeLinkGraph,
  resolveContextLink,
  rewriteWikilinks,
  splitFrontmatter,
  validateNewContextPath,
} from "./contextPaths";

function file(path: string): ContextFileEntry {
  return {
    path,
    size: 1,
    mtime: 0,
    description: null,
    always_loaded: path.startsWith("system/"),
    writable: true,
    readable: true,
  };
}

const FILES = [
  file("CONTEXT.md"),
  file("system/rules.md"),
  file("wiki/exp.md"),
  file("wiki/a/b.md"),
];

describe("splitFrontmatter", () => {
  it("separates YAML frontmatter from the body", () => {
    expect(splitFrontmatter("---\ndescription: x\n---\n# Hi")).toEqual({
      frontmatter: "description: x",
      body: "# Hi",
    });
  });
  it("leaves documents without frontmatter untouched", () => {
    expect(splitFrontmatter("# Hi\n---\n")).toEqual({ frontmatter: null, body: "# Hi\n---\n" });
  });
});

describe("rewriteWikilinks", () => {
  it("turns [[target|alias]] and [[target#anchor]] into scheme links", () => {
    expect(rewriteWikilinks("see [[exp]] and [[rules|the rules]] and [[b#sec]]")).toBe(
      `see [exp](${WIKILINK_SCHEME}exp) and [the rules](${WIKILINK_SCHEME}rules) and [b](${WIKILINK_SCHEME}b)`,
    );
  });
});

describe("resolveContextLink", () => {
  it("resolves wikilinks by stem or path", () => {
    expect(resolveContextLink(`${WIKILINK_SCHEME}exp`, "system/rules.md", FILES)).toBe(
      "wiki/exp.md",
    );
    expect(resolveContextLink(`${WIKILINK_SCHEME}wiki%2Fa%2Fb`, "wiki/exp.md", FILES)).toBe(
      "wiki/a/b.md",
    );
    expect(resolveContextLink(`${WIKILINK_SCHEME}missing`, "wiki/exp.md", FILES)).toBeNull();
  });
  it("resolves relative markdown links and ignores external ones", () => {
    expect(resolveContextLink("../system/rules.md", "wiki/exp.md", FILES)).toBe("system/rules.md");
    expect(resolveContextLink("a/b.md#x", "wiki/exp.md", FILES)).toBe("wiki/a/b.md");
    expect(resolveContextLink("https://example.com", "wiki/exp.md", FILES)).toBeNull();
    expect(resolveContextLink("/etc/passwd", "wiki/exp.md", FILES)).toBeNull();
  });
});

describe("buildContextTree", () => {
  it("nests folders, keeps standard folders first, and lists empty ones", () => {
    const tree = buildContextTree([...FILES, file("wiki/0-intro.md"), file("notes/x.md")]);
    expect(tree.files.map((f) => f.path)).toEqual(["CONTEXT.md"]);
    expect(tree.folders.map((f) => f.name)).toEqual(["system", "wiki", "raw", "graph", "notes"]);
    const wiki = tree.folders[1]!;
    expect(wiki.files.map((f) => f.path)).toEqual(["wiki/0-intro.md", "wiki/exp.md"]);
    expect(wiki.folders.map((f) => f.path)).toEqual(["wiki/a"]);
    expect(wiki.folders[0]!.files.map((f) => f.path)).toEqual(["wiki/a/b.md"]);
    expect(tree.folders[2]!.files).toEqual([]);
  });
});

describe("defaultContextFile", () => {
  it("prefers CONTEXT.md, then always-loaded, then any markdown", () => {
    expect(defaultContextFile(FILES)).toBe("CONTEXT.md");
    expect(defaultContextFile(FILES.slice(1))).toBe("system/rules.md");
    expect(defaultContextFile([file("wiki/z.md"), file("raw/a.txt")])).toBe("wiki/z.md");
    expect(defaultContextFile([{ ...file("wiki/z.md"), readable: false }])).toBeNull();
  });
});

describe("parseNodeLinkGraph", () => {
  it("reads node-link JSON with links or edges and drops dangling edges", () => {
    const graph = parseNodeLinkGraph(
      JSON.stringify({
        nodes: [{ id: "a", label: "A", community: 2 }, { id: 1 }],
        links: [
          { source: "a", target: 1, relation: "calls" },
          { source: "a", target: "missing" },
        ],
      }),
    );
    expect(graph?.nodes.map((n) => [n.id, n.label, n.community])).toEqual([
      ["a", "A", 2],
      ["1", "1", null],
    ]);
    expect(graph?.edges).toEqual([{ source: "a", target: "1", relation: "calls" }]);
    expect(parseNodeLinkGraph('{"nodes": [], "edges": []}')).toEqual({
      nodes: [],
      edges: [],
      truncated: false,
    });
  });
  it("rejects other JSON", () => {
    expect(parseNodeLinkGraph("not json")).toBeNull();
    expect(parseNodeLinkGraph('{"nodes": [1, 2]}')).toBeNull();
    expect(parseNodeLinkGraph("[1]")).toBeNull();
  });
});

describe("validateNewContextPath", () => {
  it.each([
    ["wiki/topic.md", null],
    ["system/rules.txt", null],
    ["", "Enter a path"],
    ["/abs/x.md", "Use a relative path"],
    ["wiki/../x.md", "Path segments cannot be hidden or '..'"],
    ["raw/x.md", "New files go under system/ or wiki/"],
    ["wiki/x.py", "Use a .md or .txt file name"],
  ])("%s → %s", (path, expected) => {
    expect(validateNewContextPath(path)).toBe(expected);
  });
});
