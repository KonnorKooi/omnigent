// A small, dependency-free force-directed layout for the Context page graph.
//
// Why not @xyflow/react: it renders every node as a DOM element and has no
// force layout, which does not hold up at the 1–2k node graphs graphify emits.
// Why not a force-graph package: the needs here are modest (spring edges,
// repulsion, centering, cooling) and a ~150-line simulation over typed arrays
// keeps the bundle and lockfile unchanged. Repulsion uses a uniform grid so a
// tick is ~O(n) instead of O(n²); nodes only repel neighbours within a few
// cells, which is plenty for a navigation view.

export interface LayoutInput {
  /** Node count; node i is addressed by its index. */
  count: number;
  /** Edge endpoints as index pairs. */
  edges: readonly (readonly [number, number])[];
}

export interface LayoutState {
  x: Float32Array;
  y: Float32Array;
  vx: Float32Array;
  vy: Float32Array;
  /** Remaining "temperature" in [0, 1]; the simulation stops at ~0. */
  alpha: number;
}

const LINK_DISTANCE = 40;
const LINK_STRENGTH = 0.08;
const REPULSION = 900;
const CELL = 90;
const CENTER_STRENGTH = 0.004;
const DAMPING = 0.6;
const ALPHA_DECAY = 0.985;
const ALPHA_MIN = 0.002;

/**
 * Seed positions on a deterministic phyllotaxis spiral so the first frame is
 * already spread out and repeated renders of the same graph look the same.
 */
export function createLayout(count: number): LayoutState {
  const x = new Float32Array(count);
  const y = new Float32Array(count);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const r = 12 * Math.sqrt(i + 0.5);
    x[i] = r * Math.cos(i * golden);
    y[i] = r * Math.sin(i * golden);
  }
  return { x, y, vx: new Float32Array(count), vy: new Float32Array(count), alpha: 1 };
}

/**
 * Advance the simulation one step in place.
 *
 * @param state Positions/velocities, mutated.
 * @param input Graph topology.
 * @returns `true` while the layout is still moving meaningfully.
 */
export function tickLayout(state: LayoutState, input: LayoutInput): boolean {
  const { x, y, vx, vy } = state;
  const n = input.count;
  if (n === 0 || state.alpha < ALPHA_MIN) return false;
  const alpha = state.alpha;

  // Grid-bucketed repulsion.
  const buckets = new Map<number, number[]>();
  const keyOf = (cx: number, cy: number) => cx * 73_856_093 + cy * 19_349_663;
  for (let i = 0; i < n; i++) {
    const key = keyOf(Math.floor(x[i]! / CELL), Math.floor(y[i]! / CELL));
    const bucket = buckets.get(key);
    if (bucket) bucket.push(i);
    else buckets.set(key, [i]);
  }
  for (let i = 0; i < n; i++) {
    const cx = Math.floor(x[i]! / CELL);
    const cy = Math.floor(y[i]! / CELL);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        const bucket = buckets.get(keyOf(cx + dx, cy + dy));
        if (!bucket) continue;
        for (const j of bucket) {
          if (j <= i) continue;
          let ddx = x[i]! - x[j]!;
          let ddy = y[i]! - y[j]!;
          let dist2 = ddx * ddx + ddy * ddy;
          if (dist2 < 0.01) {
            // Coincident nodes: nudge apart deterministically.
            ddx = ((i % 7) - 3) * 0.1 + 0.05;
            ddy = ((j % 5) - 2) * 0.1 + 0.05;
            dist2 = ddx * ddx + ddy * ddy;
          }
          const force = (REPULSION * alpha) / dist2;
          const dist = Math.sqrt(dist2);
          const fx = (ddx / dist) * force;
          const fy = (ddy / dist) * force;
          vx[i]! += fx;
          vy[i]! += fy;
          vx[j]! -= fx;
          vy[j]! -= fy;
        }
      }
    }
  }

  // Springs along edges.
  for (const [a, b] of input.edges) {
    const ddx = x[b]! - x[a]!;
    const ddy = y[b]! - y[a]!;
    const dist = Math.sqrt(ddx * ddx + ddy * ddy) || 0.01;
    const force = (dist - LINK_DISTANCE) * LINK_STRENGTH * alpha;
    const fx = (ddx / dist) * force;
    const fy = (ddy / dist) * force;
    vx[a]! += fx;
    vy[a]! += fy;
    vx[b]! -= fx;
    vy[b]! -= fy;
  }

  // Centering + integrate.
  let movement = 0;
  for (let i = 0; i < n; i++) {
    vx[i]! -= x[i]! * CENTER_STRENGTH * alpha;
    vy[i]! -= y[i]! * CENTER_STRENGTH * alpha;
    vx[i]! *= DAMPING;
    vy[i]! *= DAMPING;
    x[i]! += vx[i]!;
    y[i]! += vy[i]!;
    movement += Math.abs(vx[i]!) + Math.abs(vy[i]!);
  }
  state.alpha *= ALPHA_DECAY;
  return state.alpha >= ALPHA_MIN && movement / n > 0.001;
}

/** Axis-aligned bounds of the current layout (for fit-to-view). */
export function layoutBounds(state: LayoutState): {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
} {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < state.x.length; i++) {
    minX = Math.min(minX, state.x[i]!);
    maxX = Math.max(maxX, state.x[i]!);
    minY = Math.min(minY, state.y[i]!);
    maxY = Math.max(maxY, state.y[i]!);
  }
  if (!Number.isFinite(minX)) return { minX: -1, minY: -1, maxX: 1, maxY: 1 };
  return { minX, minY, maxX, maxY };
}

/** A stable categorical colour for a group key (community id or folder). */
export function colorForGroup(key: string | number | null | undefined): string {
  if (key === null || key === undefined) return "hsl(220 10% 55%)";
  const text = String(key);
  let hash = 0;
  for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) | 0;
  const hue = ((hash % 360) + 360) % 360;
  return `hsl(${hue} 65% 52%)`;
}
