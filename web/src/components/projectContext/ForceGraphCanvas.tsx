// Canvas renderer for the Context page graph: pan (drag), zoom (wheel), hover
// label, click-to-select, and a highlight set for search matches. Layout comes
// from `forceLayout.ts` and runs in requestAnimationFrame until it cools.

import { useEffect, useMemo, useRef, useState } from "react";
import { colorForGroup, createLayout, layoutBounds, tickLayout } from "./forceLayout";
import type { GraphEdge, GraphNode } from "@/lib/projectContextApi";

interface ForceGraphCanvasProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Colour key per node (community for code, folder for knowledge). */
  groupOf: (node: GraphNode) => string | number | null | undefined;
  /** Ids to emphasise (search matches); others are dimmed when non-empty. */
  highlighted: ReadonlySet<string>;
  selectedId: string | null;
  /** Node to keep in view: panned to when it changes and is off-screen. */
  focusId?: string | null;
  onSelect: (node: GraphNode | null) => void;
  className?: string;
}

interface View {
  scale: number;
  tx: number;
  ty: number;
}

export function ForceGraphCanvas({
  nodes,
  edges,
  groupOf,
  highlighted,
  selectedId,
  focusId = null,
  onSelect,
  className,
}: ForceGraphCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<View>({ scale: 1, tx: 0, ty: 0 });
  const fittedRef = useRef(false);
  const [hover, setHover] = useState<{ node: GraphNode; x: number; y: number } | null>(null);

  const indexById = useMemo(() => new Map(nodes.map((n, i) => [n.id, i])), [nodes]);
  const edgeIdx = useMemo(
    () =>
      edges.flatMap((e) => {
        const a = indexById.get(e.source);
        const b = indexById.get(e.target);
        return a === undefined || b === undefined ? [] : [[a, b] as const];
      }),
    [edges, indexById],
  );
  const degrees = useMemo(() => {
    const d = new Uint32Array(nodes.length);
    for (const [a, b] of edgeIdx) {
      d[a]! += 1;
      d[b]! += 1;
    }
    return d;
  }, [edgeIdx, nodes.length]);
  const colors = useMemo(() => nodes.map((n) => colorForGroup(groupOf(n))), [nodes, groupOf]);
  const layoutRef = useRef(createLayout(nodes.length));
  const drawRef = useRef<() => void>(() => {});

  // Reset layout whenever the graph itself changes.
  useEffect(() => {
    layoutRef.current = createLayout(nodes.length);
    fittedRef.current = false;
  }, [nodes, edgeIdx]);

  const radiusOf = (i: number) => 2.5 + Math.min(8, Math.sqrt(degrees[i] ?? 0) * 1.3);

  drawRef.current = () => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    }
    const layout = layoutRef.current;
    const view = viewRef.current;
    if (!fittedRef.current && nodes.length > 0) {
      const b = layoutBounds(layout);
      const spanX = Math.max(1, b.maxX - b.minX);
      const spanY = Math.max(1, b.maxY - b.minY);
      view.scale = Math.min(2, 0.9 * Math.min(width / spanX, height / spanY));
      view.tx = width / 2 - ((b.minX + b.maxX) / 2) * view.scale;
      view.ty = height / 2 - ((b.minY + b.maxY) / 2) * view.scale;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.setTransform(dpr * view.scale, 0, 0, dpr * view.scale, dpr * view.tx, dpr * view.ty);
    const dimOthers = highlighted.size > 0;
    ctx.lineWidth = 0.6 / view.scale;
    ctx.strokeStyle = "rgba(128,128,128,0.25)";
    ctx.beginPath();
    for (const [a, b] of edgeIdx) {
      ctx.moveTo(layout.x[a]!, layout.y[a]!);
      ctx.lineTo(layout.x[b]!, layout.y[b]!);
    }
    ctx.stroke();
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i]!;
      const lit = !dimOthers || highlighted.has(node.id);
      ctx.globalAlpha = lit ? 1 : 0.18;
      ctx.fillStyle = colors[i]!;
      ctx.beginPath();
      ctx.arc(layout.x[i]!, layout.y[i]!, radiusOf(i), 0, Math.PI * 2);
      ctx.fill();
      if (node.id === selectedId) {
        ctx.lineWidth = 2 / view.scale;
        ctx.strokeStyle = "rgba(255,255,255,0.95)";
        ctx.stroke();
        ctx.lineWidth = 0.6 / view.scale;
        ctx.strokeStyle = "rgba(128,128,128,0.25)";
      }
    }
    ctx.globalAlpha = 1;
    // Labels for highlighted/selected nodes, and all nodes when zoomed in.
    ctx.font = `${11 / view.scale}px ui-sans-serif, system-ui, sans-serif`;
    ctx.fillStyle = getComputedStyle(container).color || "#888";
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i]!;
      const show =
        node.id === selectedId ||
        (dimOthers && highlighted.has(node.id)) ||
        view.scale > 1.6 ||
        (degrees[i] ?? 0) > 25;
      if (show) ctx.fillText(node.label, layout.x[i]! + radiusOf(i) + 2, layout.y[i]! + 3);
    }
  };

  const focusRef = useRef<string | null>(focusId);
  const applyFocus = () => {
    const idx = focusRef.current === null ? undefined : indexById.get(focusRef.current);
    const container = containerRef.current;
    if (idx === undefined || !container) return;
    const view = viewRef.current;
    const layout = layoutRef.current;
    const sx = layout.x[idx]! * view.scale + view.tx;
    const sy = layout.y[idx]! * view.scale + view.ty;
    const w = container.clientWidth;
    const h = container.clientHeight;
    const margin = 24;
    if (sx >= margin && sx <= w - margin && sy >= margin && sy <= h - margin) return;
    view.tx = w / 2 - layout.x[idx]! * view.scale;
    view.ty = h / 2 - layout.y[idx]! * view.scale;
    drawRef.current();
  };
  const applyFocusRef = useRef(applyFocus);
  applyFocusRef.current = applyFocus;

  // Pan to the focused node once the layout has settled.
  useEffect(() => {
    focusRef.current = focusId;
    if (fittedRef.current) applyFocusRef.current();
  }, [focusId]);

  // Redraw when the panel is resized (collapsed/expanded, window resize).
  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => drawRef.current());
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // Animation loop: tick until cooled, then redraw only on interaction.
  useEffect(() => {
    let frame = 0;
    let running = true;
    const loop = () => {
      if (!running) return;
      let moving = false;
      // A few ticks per frame converge large graphs faster without jank.
      for (let k = 0; k < 2; k++) {
        moving = tickLayout(layoutRef.current, { count: nodes.length, edges: edgeIdx }) || moving;
      }
      drawRef.current();
      if (moving) frame = requestAnimationFrame(loop);
      else {
        fittedRef.current = true;
        applyFocusRef.current();
      }
    };
    frame = requestAnimationFrame(loop);
    return () => {
      running = false;
      cancelAnimationFrame(frame);
    };
  }, [nodes, edgeIdx]);

  // Redraw on highlight/selection changes.
  useEffect(() => {
    drawRef.current();
  }, [highlighted, selectedId]);

  const toWorld = (clientX: number, clientY: number) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    const view = viewRef.current;
    return {
      wx: (clientX - rect.left - view.tx) / view.scale,
      wy: (clientY - rect.top - view.ty) / view.scale,
      sx: clientX - rect.left,
      sy: clientY - rect.top,
    };
  };

  const hitTest = (clientX: number, clientY: number): number => {
    const { wx, wy } = toWorld(clientX, clientY);
    const layout = layoutRef.current;
    let best = -1;
    let bestDist = Infinity;
    const slop = 4 / viewRef.current.scale;
    for (let i = 0; i < nodes.length; i++) {
      const dx = layout.x[i]! - wx;
      const dy = layout.y[i]! - wy;
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d <= radiusOf(i) + slop && d < bestDist) {
        best = i;
        bestDist = d;
      }
    }
    return best;
  };

  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ position: "relative", overflow: "hidden" }}
      data-testid="context-graph-canvas"
    >
      <canvas
        ref={canvasRef}
        aria-label="Context graph"
        role="img"
        onPointerDown={(e) => {
          fittedRef.current = true;
          dragRef.current = { x: e.clientX, y: e.clientY, moved: false };
          (e.target as HTMLCanvasElement).setPointerCapture?.(e.pointerId);
        }}
        onPointerMove={(e) => {
          const drag = dragRef.current;
          if (drag) {
            const dx = e.clientX - drag.x;
            const dy = e.clientY - drag.y;
            if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
            viewRef.current.tx += dx;
            viewRef.current.ty += dy;
            drag.x = e.clientX;
            drag.y = e.clientY;
            drawRef.current();
            return;
          }
          const idx = hitTest(e.clientX, e.clientY);
          if (idx < 0) setHover(null);
          else {
            const { sx, sy } = toWorld(e.clientX, e.clientY);
            setHover({ node: nodes[idx]!, x: sx, y: sy });
          }
        }}
        onPointerUp={(e) => {
          const drag = dragRef.current;
          dragRef.current = null;
          if (drag && !drag.moved) {
            const idx = hitTest(e.clientX, e.clientY);
            onSelect(idx >= 0 ? nodes[idx]! : null);
          }
        }}
        onWheel={(e) => {
          fittedRef.current = true;
          const { sx, sy } = toWorld(e.clientX, e.clientY);
          const view = viewRef.current;
          const factor = Math.exp(-e.deltaY * 0.0015);
          const next = Math.min(12, Math.max(0.05, view.scale * factor));
          view.tx = sx - ((sx - view.tx) * next) / view.scale;
          view.ty = sy - ((sy - view.ty) * next) / view.scale;
          view.scale = next;
          drawRef.current();
        }}
      />
      {hover && (
        <div
          className="pointer-events-none absolute z-10 max-w-xs rounded-md border bg-popover px-2 py-1 text-xs text-popover-foreground shadow"
          style={{ left: hover.x + 12, top: hover.y + 12 }}
        >
          <div className="font-medium">{hover.node.label}</div>
          {hover.node.source_file && (
            <div className="text-muted-foreground">
              {hover.node.source_file}
              {hover.node.source_location ? `:${hover.node.source_location}` : ""}
            </div>
          )}
          {hover.node.description && (
            <div className="text-muted-foreground">{hover.node.description}</div>
          )}
        </div>
      )}
    </div>
  );
}
