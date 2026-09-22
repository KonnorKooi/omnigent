// Graph side panel of the Context page and the viewer for graph JSON files:
// a force-directed canvas with search highlighting and a neighbours panel.
// The panel opens on the code graph when one exists (knowledge graph
// otherwise) and keeps the open markdown file highlighted.

import { type ReactNode, useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Maximize2Icon, Minimize2Icon, PanelRightCloseIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { type GraphEdge, type GraphNode, fetchContextGraph } from "@/lib/projectContextApi";
import { contextQueryKey } from "./contextQueryKey";
import { ForceGraphCanvas } from "./ForceGraphCanvas";

export type GraphKind = "knowledge" | "code";

/** Case-insensitive label/id/path match used for search highlighting. */
export function matchGraphNodes(nodes: readonly GraphNode[], query: string): Set<string> {
  const q = query.trim().toLowerCase();
  if (!q) return new Set();
  return new Set(
    nodes
      .filter(
        (n) =>
          n.label.toLowerCase().includes(q) ||
          n.id.toLowerCase().includes(q) ||
          (n.source_file ?? "").toLowerCase().includes(q),
      )
      .map((n) => n.id),
  );
}

const codeGroup = (node: GraphNode) => node.community ?? node.group ?? null;
const knowledgeGroup = (node: GraphNode) => node.group ?? null;

/**
 * Search box, canvas and selected-node details over one graph. With
 * `onOpenNode`, clicking a node hands it off (knowledge graph → open file)
 * instead of showing its neighbours.
 */
export function GraphExplorer({
  nodes,
  edges,
  kind,
  activeId = null,
  onOpenNode,
  toolbar,
  summary,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  kind: GraphKind;
  activeId?: string | null;
  onOpenNode?: (node: GraphNode) => void;
  toolbar?: ReactNode;
  summary?: ReactNode;
}) {
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const highlighted = useMemo(() => matchGraphNodes(nodes, search), [nodes, search]);
  const groupOf = kind === "code" ? codeGroup : knowledgeGroup;

  const neighbors = useMemo(() => {
    if (!selected) return [];
    const byId = new Map(nodes.map((n) => [n.id, n]));
    return edges
      .filter((e) => e.source === selected.id || e.target === selected.id)
      .map((e) => {
        const out = e.source === selected.id;
        return { node: byId.get(out ? e.target : e.source), relation: e.relation, out };
      })
      .filter((row): row is { node: GraphNode; relation: string; out: boolean } =>
        Boolean(row.node),
      );
  }, [selected, nodes, edges]);

  const selectedId = onOpenNode ? activeId : (selected?.id ?? null);
  const onSelect = useCallback(
    (node: GraphNode | null) => {
      if (node && onOpenNode) onOpenNode(node);
      else setSelected(node);
    },
    [onOpenNode],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2" data-testid="context-graph">
      <div className="flex flex-wrap items-center gap-2">
        {toolbar}
        <Input
          aria-label="Search graph"
          placeholder="Search nodes…"
          className="h-8 w-40 flex-1"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {search && (
          <span className="text-xs text-muted-foreground">{highlighted.size} matches</span>
        )}
      </div>
      {summary && <div className="text-xs text-muted-foreground">{summary}</div>}
      <ForceGraphCanvas
        className="min-h-[16rem] flex-1 rounded-md border text-foreground"
        nodes={nodes}
        edges={edges}
        groupOf={groupOf}
        highlighted={highlighted}
        selectedId={selectedId}
        focusId={selectedId}
        onSelect={onSelect}
      />
      {selected && !onOpenNode && (
        <aside
          className="max-h-60 shrink-0 overflow-y-auto rounded-md border p-3 text-sm"
          data-testid="context-graph-details"
        >
          <div className="font-medium break-all">{selected.label}</div>
          {selected.source_file && (
            <div className="font-mono text-xs break-all text-muted-foreground">
              {selected.source_file}
              {selected.source_location ? `:${selected.source_location}` : ""}
            </div>
          )}
          {selected.community_name && (
            <div className="mt-1 text-xs text-muted-foreground">
              Community: {selected.community_name}
            </div>
          )}
          <div className="mt-3 mb-1 text-xs font-medium text-muted-foreground uppercase">
            Neighbours ({neighbors.length})
          </div>
          <ul className="flex flex-col gap-1">
            {neighbors.map(({ node, relation, out }) => (
              <li key={`${node.id}-${relation}-${out}`}>
                <button
                  type="button"
                  className="text-left hover:underline"
                  onClick={() => setSelected(node)}
                >
                  <span className="text-muted-foreground">
                    {out ? "→" : "←"} {relation}
                  </span>{" "}
                  {node.label}
                </button>
              </li>
            ))}
          </ul>
        </aside>
      )}
    </div>
  );
}

/** The Context page's graph panel. */
export function ContextGraphPanel({
  projectId,
  hasCodeGraph,
  activePath,
  onOpenFile,
  expanded,
  onToggleExpanded,
  onCollapse,
}: {
  projectId: string;
  hasCodeGraph: boolean;
  activePath: string | null;
  onOpenFile: (path: string) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  onCollapse: () => void;
}) {
  const [picked, setPicked] = useState<GraphKind | null>(null);
  const kind: GraphKind = picked ?? (hasCodeGraph ? "code" : "knowledge");
  const { data, isLoading, error } = useQuery({
    queryKey: contextQueryKey(projectId, "graph", kind),
    queryFn: () => fetchContextGraph(projectId, kind),
  });
  const nodes = useMemo(() => data?.nodes ?? [], [data]);
  const edges = useMemo(() => data?.edges ?? [], [data]);
  const openNode = useCallback((node: GraphNode) => onOpenFile(node.id), [onOpenFile]);

  const toolbar = (
    <div className="flex items-center gap-1">
      {(["code", "knowledge"] as const).map((k) => (
        <Button
          key={k}
          size="sm"
          variant={kind === k ? "secondary" : "ghost"}
          onClick={() => setPicked(k)}
        >
          {k === "knowledge" ? "Knowledge" : "Code"}
        </Button>
      ))}
    </div>
  );

  let body: ReactNode;
  if (isLoading) body = <Spinner />;
  else if (error) body = <p className="text-sm text-destructive">{String(error)}</p>;
  else if (kind === "code" && data?.available === false)
    body = (
      <p className="text-sm text-muted-foreground">
        No code graph yet. Set a code repository in the context settings and run Update context to
        build one with graphify.
      </p>
    );
  else if (nodes.length === 0)
    body = (
      <p className="text-sm text-muted-foreground">
        No markdown files to graph yet. Link pages with [[wikilinks]] to see connections.
      </p>
    );
  else
    body = (
      <GraphExplorer
        key={kind}
        nodes={nodes}
        edges={edges}
        kind={kind}
        activeId={kind === "knowledge" ? activePath : null}
        onOpenNode={kind === "knowledge" ? openNode : undefined}
        summary={
          <>
            {nodes.length.toLocaleString()} nodes · {edges.length.toLocaleString()} edges
            {data?.truncated && data.total_nodes ? (
              <>
                {" "}
                (showing top {nodes.length} of {data.total_nodes.toLocaleString()} by degree){" "}
                <Badge variant="outline">truncated</Badge>
              </>
            ) : null}
          </>
        }
      />
    );

  return (
    <section className="flex min-h-0 flex-1 flex-col gap-2" data-testid="context-graph-panel">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">Graph</span>
        {toolbar}
        <div className="ml-auto flex items-center gap-1">
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={expanded ? "Shrink graph" : "Expand graph"}
            onClick={onToggleExpanded}
          >
            {expanded ? (
              <Minimize2Icon className="size-3.5" />
            ) : (
              <Maximize2Icon className="size-3.5" />
            )}
          </Button>
          <Button size="icon-sm" variant="ghost" aria-label="Hide graph" onClick={onCollapse}>
            <PanelRightCloseIcon className="size-3.5" />
          </Button>
        </div>
      </div>
      {body}
    </section>
  );
}
