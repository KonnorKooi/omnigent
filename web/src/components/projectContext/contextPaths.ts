// Pure helpers for the Context page: frontmatter splitting, wikilink rewriting,
// link resolution, and tree grouping. Kept free of React so they are cheap to
// unit test.

import type { ContextFileEntry, GraphEdge, GraphNode } from "@/lib/projectContextApi";

/** Top-level folders in display order. */
export const CONTEXT_FOLDERS = ["system", "wiki", "raw", "graph"] as const;
export type ContextFolder = (typeof CONTEXT_FOLDERS)[number];

/** Folders whose markdown can be created/edited/renamed/deleted. */
export const WRITABLE_FOLDERS: readonly ContextFolder[] = ["system", "wiki"];

/** Split leading YAML frontmatter (`---` … `---`) from a markdown body. */
export function splitFrontmatter(text: string): { frontmatter: string | null; body: string } {
  if (!text.startsWith("---")) return { frontmatter: null, body: text };
  const lines = text.split("\n");
  if (lines[0]!.trim() !== "---") return { frontmatter: null, body: text };
  for (let i = 1; i < Math.min(lines.length, 200); i++) {
    const line = lines[i]!.trim();
    if (line === "---" || line === "...") {
      return { frontmatter: lines.slice(1, i).join("\n"), body: lines.slice(i + 1).join("\n") };
    }
  }
  return { frontmatter: null, body: text };
}

/** URL scheme used internally for rewritten `[[wikilinks]]`. */
export const WIKILINK_SCHEME = "wikilink:";

/**
 * Rewrite `[[target]]`, `[[target|alias]]` and `[[target#anchor]]` into
 * markdown links with the internal wikilink scheme so react-markdown renders
 * them as anchors the page can intercept.
 */
export function rewriteWikilinks(markdown: string): string {
  return markdown.replace(
    /\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]/g,
    (_match, target: string, alias: string | undefined) => {
      const label = (alias ?? target).trim().replace(/[[\]]/g, "");
      return `[${label}](${WIKILINK_SCHEME}${encodeURIComponent(target.trim())})`;
    },
  );
}

/**
 * Resolve a link found in `fromPath` to a context file path, or `null` when it
 * points outside the known files (external URLs, missing pages).
 */
export function resolveContextLink(
  href: string,
  fromPath: string,
  files: readonly ContextFileEntry[],
): string | null {
  const known = new Set(files.map((f) => f.path));
  if (href.startsWith(WIKILINK_SCHEME)) {
    const target = decodeURIComponent(href.slice(WIKILINK_SCHEME.length))
      .toLowerCase()
      .replace(/\.md$/, "");
    const byPath = files.find((f) => f.path.toLowerCase().replace(/\.md$/, "") === target);
    if (byPath) return byPath.path;
    const stem = target.split("/").pop();
    const byStem = files
      .filter((f) => f.path.toLowerCase().endsWith(".md"))
      .filter((f) => (f.path.split("/").pop() ?? "").toLowerCase().replace(/\.md$/, "") === stem)
      .map((f) => f.path)
      .sort();
    return byStem[0] ?? null;
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("/") || href.startsWith("#")) {
    return null;
  }
  const clean = href.split("#")[0]!;
  const parts = fromPath.split("/").slice(0, -1);
  for (const segment of clean.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") parts.pop();
    else parts.push(segment);
  }
  const joined = parts.join("/");
  return known.has(joined) ? joined : null;
}

/** A folder in the nested file tree; `path` is `""` for the root. */
export interface ContextTreeFolder {
  name: string;
  path: string;
  folders: ContextTreeFolder[];
  files: ContextFileEntry[];
}

/**
 * Nest the server's flat file list into folders. The standard top-level
 * folders always appear (in `CONTEXT_FOLDERS` order, even when empty), other
 * folders and files sort by name.
 */
export function buildContextTree(files: readonly ContextFileEntry[]): ContextTreeFolder {
  const root: ContextTreeFolder = { name: "", path: "", folders: [], files: [] };
  const folderAt = (parent: ContextTreeFolder, name: string): ContextTreeFolder => {
    let folder = parent.folders.find((f) => f.name === name);
    if (!folder) {
      const path = parent.path ? `${parent.path}/${name}` : name;
      folder = { name, path, folders: [], files: [] };
      parent.folders.push(folder);
    }
    return folder;
  };
  for (const name of CONTEXT_FOLDERS) folderAt(root, name);
  for (const file of files) {
    const segments = file.path.split("/");
    let folder = root;
    for (const segment of segments.slice(0, -1)) folder = folderAt(folder, segment);
    folder.files.push(file);
  }
  const top = new Map<string, number>(CONTEXT_FOLDERS.map((name, i) => [name, i]));
  const sortFolder = (folder: ContextTreeFolder, isRoot: boolean) => {
    folder.folders.sort((a, b) => {
      const ra = isRoot ? (top.get(a.name) ?? top.size) : 0;
      const rb = isRoot ? (top.get(b.name) ?? top.size) : 0;
      return ra - rb || a.name.localeCompare(b.name);
    });
    folder.files.sort((a, b) => a.path.localeCompare(b.path));
    for (const child of folder.folders) sortFolder(child, false);
  };
  sortFolder(root, true);
  return root;
}

/** The last path segment. */
export function baseName(path: string): string {
  return path.split("/").pop() ?? path;
}

/**
 * File to open when the page loads: `CONTEXT.md`, else the first always-loaded
 * markdown, else any readable markdown.
 */
export function defaultContextFile(files: readonly ContextFileEntry[]): string | null {
  const md = files
    .filter((f) => f.readable && f.path.toLowerCase().endsWith(".md"))
    .sort((a, b) => a.path.localeCompare(b.path));
  return (
    md.find((f) => f.path === "CONTEXT.md")?.path ??
    md.find((f) => f.always_loaded)?.path ??
    md[0]?.path ??
    null
  );
}

/** The graphify output the server serves through the code-graph endpoint. */
export const CODE_GRAPH_FILE = "graph/graph.json";

/** Most nodes drawn for a graph parsed from a file. */
export const MAX_FILE_GRAPH_NODES = 2000;

/**
 * Parse a node-link graph JSON (`{nodes: [{id}], links|edges: [{source, target}]}`),
 * or return `null` when the text is not one.
 */
export function parseNodeLinkGraph(
  text: string,
): { nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean } | null {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const obj = data as { nodes?: unknown; links?: unknown; edges?: unknown };
  const rawEdges = Array.isArray(obj.links) ? obj.links : obj.edges;
  if (!Array.isArray(obj.nodes) || !Array.isArray(rawEdges)) return null;
  const endpoint = (v: unknown) =>
    typeof v === "string" || typeof v === "number"
      ? String(v)
      : v && typeof v === "object" && "id" in v
        ? String((v as { id: unknown }).id)
        : null;
  const nodes: GraphNode[] = [];
  for (const raw of obj.nodes.slice(0, MAX_FILE_GRAPH_NODES)) {
    if (!raw || typeof raw !== "object" || !("id" in raw)) return null;
    const n = raw as Record<string, unknown>;
    nodes.push({
      id: String(n.id),
      label: typeof n.label === "string" ? n.label : String(n.id),
      community: typeof n.community === "number" ? n.community : null,
      group: typeof n.group === "string" ? n.group : undefined,
      source_file: typeof n.source_file === "string" ? n.source_file : null,
      source_location: typeof n.source_location === "string" ? n.source_location : null,
    });
  }
  const ids = new Set(nodes.map((n) => n.id));
  const edges: GraphEdge[] = [];
  for (const raw of rawEdges) {
    if (!raw || typeof raw !== "object") continue;
    const e = raw as Record<string, unknown>;
    const source = endpoint(e.source);
    const target = endpoint(e.target);
    if (source === null || target === null || !ids.has(source) || !ids.has(target)) continue;
    edges.push({ source, target, relation: typeof e.relation === "string" ? e.relation : "" });
  }
  return { nodes, edges, truncated: obj.nodes.length > nodes.length };
}

/**
 * Validate a new file path typed by the user. Mirrors the server's rules so
 * the dialog can explain problems before a round trip.
 *
 * @returns An error message, or `null` when acceptable.
 */
export function validateNewContextPath(path: string): string | null {
  const trimmed = path.trim();
  if (!trimmed) return "Enter a path";
  if (trimmed.startsWith("/") || trimmed.includes("\\")) return "Use a relative path";
  const segments = trimmed.split("/");
  if (segments.some((s) => s === ".." || s.startsWith("."))) {
    return "Path segments cannot be hidden or '..'";
  }
  if (!WRITABLE_FOLDERS.includes(segments[0] as ContextFolder) || segments.length < 2) {
    return "New files go under system/ or wiki/";
  }
  if (!/\.(md|txt)$/i.test(trimmed)) return "Use a .md or .txt file name";
  return null;
}

/** Human-readable byte size. */
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
