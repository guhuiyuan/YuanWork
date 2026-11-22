/**
 * The graph's own arithmetic, kept out of the drawing.
 *
 * The layout used to live inside the component and be rebuilt every time
 * the reader selected a node — the whole SVG and its force simulation
 * went with it, so the node the reader had just clicked ran away from
 * the pointer (ADR-055). Here it is a plain function instead:
 *
 * - the same graph always gives the same positions (nothing random in
 *   it, and the order of the nodes does not matter);
 * - the selection is not an input at all, so clicking cannot move
 *   anything (AC-F-034);
 * - the place the reader dragged a node to is kept in the store and
 *   wins over the computed position;
 * - the names for the node kinds and the edge kinds live here too, so
 *   the legend and the tooltips say the same thing.
 */

export const VIEW_WIDTH = 1000;
export const VIEW_HEIGHT = 700;

/** A node as the backend sends it. */
export interface GraphNodeLike {
  id: string;
  label: string;
  type: string;
  category?: string | null;
  weight?: number;
  /** What the page says, in short. */
  key_points?: string[] | null;
  /** The other names an entity goes by. */
  aliases?: string[] | null;
  /** The pages this node is connected to. */
  related_pages?: string[] | null;
}

/** An edge as the backend sends it. */
export interface GraphEdgeLike {
  source: string;
  target: string;
  type: string;
  weight?: number;
}

export interface GraphData {
  nodes: GraphNodeLike[];
  edges: GraphEdgeLike[];
}

export interface Position {
  x: number;
  y: number;
}

export type NodeKind = "page" | "document" | "entity";

/** What kind of thing a node is. */
export function nodeKind(type: string): NodeKind {
  if (type === "page") return "page";
  if (type === "document") return "document";
  return "entity";
}

/** The kinds of entity the library knows about. */
const ENTITY_KIND_LABELS: Record<string, string> = {
  person: "人物",
  project: "项目",
  concept: "概念",
  contract: "合同",
  law: "法规",
  org: "机构",
  location: "地点",
  other: "其他",
};

export const NODE_TYPE_LABELS: Record<string, string> = {
  page: "页面",
  document: "源文件",
  entity_person: "人物",
  entity_project: "项目",
  entity_concept: "概念",
  entity_contract: "合同",
  entity_law: "法规",
  entity_org: "机构",
  entity_location: "地点",
  entity_other: "其他",
};

/** The name of a node kind, in words the reader thinks in. */
export function nodeTypeLabel(type: string): string {
  const known = NODE_TYPE_LABELS[type];
  if (known) return known;
  if (type.startsWith("entity_")) {
    const kind = type.slice("entity_".length);
    return ENTITY_KIND_LABELS[kind] ?? `实体·${kind}`;
  }
  return type;
}

export const NODE_COLORS: Record<string, string> = {
  page: "#2563eb",
  document: "#475569",
  entity_person: "#16a34a",
  entity_project: "#f59e0b",
  entity_concept: "#9333ea",
  entity_contract: "#dc2626",
  entity_law: "#0891b2",
  entity_org: "#0f766e",
  entity_location: "#65a30d",
  entity_other: "#6b7280",
};

export function nodeColor(type: string): string {
  return NODE_COLORS[type] ?? "#6b7280";
}

export function nodeRadius(type: string): number {
  if (type === "page") return 13;
  if (type === "document") return 10;
  return 8;
}

/**
 * What each edge means, in one word for the legend and in a sentence
 * for the tooltip. Every edge the backend draws has to be sayable
 * (ADR-054) — an edge nobody can explain is a guess, and the reader
 * is in no position to judge a guess.
 */
export const EDGE_TYPE_LABELS: Record<string, string> = {
  derived_from: "出自",
  references: "链接",
  shares_evidence: "共享来源",
  mentions: "提到",
  co_occurs: "共现",
};

export const EDGE_TYPE_HINTS: Record<string, string> = {
  derived_from: "这一页由这份文件编译出来",
  references: "这一页链接到另一页",
  shares_evidence: "两页出自同一批材料",
  mentions: "这一页提到了这个实体",
  co_occurs: "两个实体在同一页出现过",
};

export const EDGE_COLORS: Record<string, string> = {
  derived_from: "#64748b",
  references: "#2563eb",
  shares_evidence: "#16a34a",
  mentions: "#f59e0b",
  co_occurs: "#9333ea",
};

export function edgeTypeLabel(type: string): string {
  const known = EDGE_TYPE_LABELS[type];
  if (known) return known;
  // A kind of edge nobody can explain is a guess, and the reader is
  // in no position to judge a guess (ADR-054). Saying 「有关系」would
  // hide that, so the unknown kind is named: the reader can see that
  // there is a relation here which has not been worked out yet.
  return `未知关系·${type}`;
}

export function edgeTypeHint(type: string): string {
  const known = EDGE_TYPE_HINTS[type];
  if (known) return known;
  return `一条还没有说明是什么的关系（${type}）`;
}

export function edgeColor(type: string): string {
  return EDGE_COLORS[type] ?? "#94a3b8";
}

/** How far apart two connected nodes want to sit. */
const REST_LENGTH: Record<string, number> = {
  derived_from: 95,
  references: 120,
  shares_evidence: 90,
  mentions: 70,
  co_occurs: 60,
};

// ===== the layout =====

export interface LayoutNode extends GraphNodeLike {
  x: number;
  y: number;
  radius: number;
  color: string;
  kind: NodeKind;
}

export interface LayoutEdge {
  source: string;
  target: string;
  type: string;
  weight: number;
}

export interface GraphLayout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
}

export interface LayoutOptions {
  width?: number;
  height?: number;
}

/** How long the simulation runs. The same number every time. */
const ITERATIONS = 260;
/** How hard two nodes push each other apart. */
const REPULSION = 5200;
/** How hard a connected pair pulls each other together. */
const ATTRACTION = 0.06;
/** How hard the whole thing is pulled back to the middle. */
const GRAVITY = 0.012;
/** How far a node may move in one step. */
const MAX_STEP = 26;
/** How far apart two nodes still push each other apart. */
const REACH = 260;
/**
 * The size of graph the reach above was measured against.
 *
 * Beyond it the reach is narrowed: the push between two nodes falls
 * off with the square of the distance, so on a big graph the pairs
 * that decide the shape are the close ones. Narrowing it keeps the
 * number of pairs the same per node, and with it the time the
 * drawing takes on the main thread - the graph is an entrance, not a
 * place to wait.
 */
const REFERENCE_NODES = 160;
/** The reach never gets narrower than this. */
const MIN_REACH = 90;

/** How far apart two nodes still push each other apart, for a graph
 *  of the given size. */
function reachFor(count: number): number {
  if (count <= REFERENCE_NODES) return REACH;
  const narrowed = Math.round(REACH * Math.sqrt(REFERENCE_NODES / count));
  return Math.max(MIN_REACH, narrowed);
}

/** The cell of the grid a coordinate falls in. */
function cellOf(value: number, reach: number): number {
  return Math.floor(value / reach);
}

/**
 * The key of a grid cell.
 *
 * A number rather than a string: on a big graph the lookup happens a
 * few million times, and a number is quicker to hash than a string.
 * The offset keeps the key positive and apart for any coordinate a
 * drawing of this size can have.
 */
function cellKey(cx: number, cy: number): number {
  return (cx + 65536) * 131072 + (cy + 65536);
}

function compareById(a: { id: string }, b: { id: string }): number {
  if (a.id < b.id) return -1;
  if (a.id > b.id) return 1;
  return 0;
}

/**
 * The nodes, one each, in a fixed order.
 *
 * The backend already keeps the ids apart, but the drawing code must
 * not depend on that: a node drawn twice is one node the reader cannot
 * tell from the other.
 */
function uniqueNodes(nodes: GraphNodeLike[]): GraphNodeLike[] {
  const byId = new Map<string, GraphNodeLike>();
  for (const node of [...nodes].sort(compareById)) {
    if (!byId.has(node.id)) byId.set(node.id, node);
  }
  return [...byId.values()];
}

/** The edges that point at nodes which are really there. */
function uniqueEdges(
  edges: GraphEdgeLike[],
  nodes: GraphNodeLike[]
): GraphEdgeLike[] {
  const known = new Set(nodes.map((n) => n.id));
  const seen = new Set<string>();
  const kept: GraphEdgeLike[] = [];
  for (const edge of [...edges].sort(
    (a, b) =>
      compareById({ id: a.source + " " + a.target + " " + a.type },
        { id: b.source + " " + b.target + " " + b.type })
  )) {
    if (!known.has(edge.source) || !known.has(edge.target)) continue;
    if (edge.source === edge.target) continue;
    const key = `${edge.source} ${edge.target} ${edge.type}`;
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(edge);
  }
  return kept;
}

/**
 * Where every node sits.
 *
 * A small force simulation of the graph's own: nodes push each other
 * apart, edges pull connected nodes together, and the whole thing is
 * pulled back to the middle. There is no random number in it — the
 * starting positions are a spiral, so the same graph always ends up in
 * the same shape.
 */
export function computeLayout(
  graph: GraphData,
  options: LayoutOptions = {}
): GraphLayout {
  const width = options.width ?? VIEW_WIDTH;
  const height = options.height ?? VIEW_HEIGHT;
  const nodes = uniqueNodes(graph.nodes);
  const edges = uniqueEdges(graph.edges, nodes);
  const count = nodes.length;

  const centreX = width / 2;
  const centreY = height / 2;

  // The starting positions: a spiral, so the nodes begin spread out
  // and evenly rather than piled on the origin.
  const golden = Math.PI * (3 - Math.sqrt(5));
  const radius = Math.min(width, height) * 0.45;
  const pos: Position[] = [];
  for (let i = 0; i < count; i++) {
    const r = radius * Math.sqrt((i + 0.5) / Math.max(1, count));
    const theta = i * golden;
    pos.push({ x: centreX + r * Math.cos(theta), y: centreY + r * Math.sin(theta) });
  }

  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const links = edges
    .map((e) => ({
      source: index.get(e.source) as number,
      target: index.get(e.target) as number,
      rest: REST_LENGTH[e.type] ?? 90,
    }))
    .filter((l) => l.source !== undefined && l.target !== undefined);

  const dx = new Float64Array(count);
  const dy = new Float64Array(count);

  const reach = reachFor(count);
  const reach2 = reach * reach;

  for (let step = 0; step < ITERATIONS; step++) {
    dx.fill(0);
    dy.fill(0);
    const cooling = 1 - step / ITERATIONS;

    // Push apart. Only the neighbours within reach matter, so the
    // pairs are looked up through a grid instead of all of them.
    const grid = new Map<number, number[]>();
    for (let i = 0; i < count; i++) {
      const key = cellKey(
        cellOf(pos[i].x, reach),
        cellOf(pos[i].y, reach)
      );
      const bucket = grid.get(key);
      if (bucket) bucket.push(i);
      else grid.set(key, [i]);
    }
    for (let i = 0; i < count; i++) {
      const cx = cellOf(pos[i].x, reach);
      const cy = cellOf(pos[i].y, reach);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const bucket = grid.get(cellKey(cx + ox, cy + oy));
          if (!bucket) continue;
          for (const j of bucket) {
            if (j <= i) continue;
            let vx = pos[j].x - pos[i].x;
            let vy = pos[j].y - pos[i].y;
            let d2 = vx * vx + vy * vy;
            if (d2 > reach2) continue;
            if (d2 < 1e-4) {
              // Two nodes on the same spot: push them apart in a
              // direction that is the same every time.
              vx = ((i % 7) - 3) * 0.5 + 0.25;
              vy = ((j % 5) - 2) * 0.5 + 0.25;
              d2 = vx * vx + vy * vy;
            }
            const d = Math.sqrt(d2);
            const f = REPULSION / d2;
            const fx = (vx / d) * f;
            const fy = (vy / d) * f;
            dx[i] -= fx;
            dy[i] -= fy;
            dx[j] += fx;
            dy[j] += fy;
          }
        }
      }
    }

    // Pull together along the edges.
    for (const link of links) {
      const vx = pos[link.target].x - pos[link.source].x;
      const vy = pos[link.target].y - pos[link.source].y;
      const d = Math.hypot(vx, vy) || 0.001;
      const f = (d - link.rest) * ATTRACTION;
      const fx = (vx / d) * f;
      const fy = (vy / d) * f;
      dx[link.source] += fx;
      dy[link.source] += fy;
      dx[link.target] -= fx;
      dy[link.target] -= fy;
    }

    // And keep the whole thing on the page.
    for (let i = 0; i < count; i++) {
      dx[i] += (centreX - pos[i].x) * GRAVITY;
      dy[i] += (centreY - pos[i].y) * GRAVITY;
    }

    // Move, a little less each round.
    const maxStep = MAX_STEP * cooling + 1;
    for (let i = 0; i < count; i++) {
      const step2 = Math.hypot(dx[i], dy[i]);
      const scale = step2 > maxStep ? maxStep / step2 : 1;
      pos[i].x += dx[i] * scale;
      pos[i].y += dy[i] * scale;
    }
  }

  const laid = fitToCanvas(pos, width, height);

  return {
    nodes: nodes.map((node, i) => ({
      ...node,
      x: laid[i].x,
      y: laid[i].y,
      radius: nodeRadius(node.type),
      color: nodeColor(node.type),
      kind: nodeKind(node.type),
    })),
    edges: edges.map((e) => ({
      source: e.source,
      target: e.target,
      type: e.type,
      weight: e.weight ?? 1,
    })),
    width,
    height,
  };
}

/** Scale and move the result so that it fills the canvas. */
function fitToCanvas(
  pos: Position[],
  width: number,
  height: number
): Position[] {
  if (pos.length === 0) return pos;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of pos) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  const spanX = maxX - minX;
  const spanY = maxY - minY;
  const pad = 60;
  if (spanX <= 0 || spanY <= 0) return pos;
  // The two directions are scaled on their own so the graph fills the
  // canvas - but not too far on their own: a hairball stretched twice
  // as wide as it is tall stops being a shape.
  const rawX = (width - pad * 2) / spanX;
  const rawY = (height - pad * 2) / spanY;
  const mean = Math.sqrt(rawX * rawY);
  const scaleX = Math.min(rawX, mean * 1.8);
  const scaleY = Math.min(rawY, mean * 1.8);
  const offsetX = (width - spanX * scaleX) / 2 - minX * scaleX;
  const offsetY = (height - spanY * scaleY) / 2 - minY * scaleY;
  return pos.map((p) => ({
    x: p.x * scaleX + offsetX,
    y: p.y * scaleY + offsetY,
  }));
}

/**
 * The place the reader dragged the nodes to.
 *
 * These win over the computed positions — the reader put them there on
 * purpose. The rest of the graph does not move: dragging one node must
 * not shuffle the others (AC-F-034).
 */
export function withSavedPositions(
  layout: GraphLayout,
  saved: Record<string, Position> = {}
): GraphLayout {
  if (!saved || Object.keys(saved).length === 0) return layout;
  return {
    ...layout,
    nodes: layout.nodes.map((node) => {
      const at = saved[node.id];
      return at ? { ...node, x: at.x, y: at.y } : node;
    }),
  };
}

/** The layout of a graph, with the reader's own positions on top. */
export function layoutGraph(
  graph: GraphData,
  saved: Record<string, Position> = {},
  options: LayoutOptions = {}
): GraphLayout {
  return withSavedPositions(computeLayout(graph, options), saved);
}

// ===== what a node is =====

/**
 * The file types the library knows, in the reader's words.
 *
 * The backend sends the suffix without the dot ("pdf", "docx"); a panel
 * that is in Chinese should not say "markdown" in the middle of a
 * Chinese sentence.
 */
const FILE_TYPE_NAMES: Record<string, string> = {
  pdf: "PDF",
  doc: "Word 文档",
  docx: "Word 文档",
  ppt: "演示文稿",
  pptx: "演示文稿",
  xls: "表格",
  xlsx: "表格",
  csv: "表格",
  md: "Markdown 文档",
  markdown: "Markdown 文档",
  txt: "文本",
  html: "网页",
  json: "JSON",
};

/** The file type as the reader would call it. */
export function fileTypeName(type: string): string {
  return FILE_TYPE_NAMES[type.toLowerCase()] ?? type;
}

export interface NodeDescription {
  /** The name of the node. */
  title: string;
  /** What kind of thing it is. */
  kind: string;
  /** The lines that say what it is — one per fact. */
  lines: string[];
}

/**
 * What a node is, in words.
 *
 * A page node says its key points — the reader sees what the page says
 * without opening it. An entity says its name, its kind, its other
 * names and the pages that mention it. A document says its file name
 * and the pages that were compiled from it.
 */
export function describeNode(
  node: GraphNodeLike,
  pageLabels: Record<string, string> = {}
): NodeDescription {
  const kind = nodeKind(node.type);
  const lines: string[] = [];

  if (kind === "page") {
    const points = (node.key_points ?? []).filter(Boolean);
    if (points.length > 0) {
      lines.push("要点");
      for (const point of points) lines.push(`· ${point}`);
    }
    const related = (node.related_pages ?? []).map((id) => pageLabels[id] ?? id);
    if (related.length > 0) lines.push(`相关页面：${related.join("、")}`);
    if (lines.length === 0) lines.push("这一页还没有要点");
    return { title: node.label, kind: "页面", lines };
  }

  if (kind === "document") {
    if (node.category) lines.push(`文件类型：${fileTypeName(node.category)}`);
    const pages = (node.related_pages ?? []).map((id) => pageLabels[id] ?? id);
    lines.push(
      pages.length > 0
        ? `由它编译出的页面：${pages.join("、")}`
        : "还没有由它编译出的页面"
    );
    return { title: node.label, kind: "源文件", lines };
  }

  const aliases = (node.aliases ?? []).filter(Boolean);
  if (aliases.length > 0) lines.push(`别名：${aliases.join("、")}`);
  const pages = (node.related_pages ?? []).map((id) => pageLabels[id] ?? id);
  lines.push(
    pages.length > 0 ? `提到它的页面：${pages.join("、")}` : "还没有页面提到它"
  );
  return { title: node.label, kind: `实体 · ${nodeTypeLabel(node.type)}`, lines };
}

// ===== what clicking a node does =====

export type NodeAction =
  /** A page has a page to open - go there. */
  | { kind: "open-page"; pageId: string }
  /** An entity or a document has no page of its own - say what it is. */
  | { kind: "describe"; nodeId: string };

/** What clicking a node should do. */
export function nodeActionFor(node: { id: string; type: string }): NodeAction {
  if (nodeKind(node.type) === "page") {
    return { kind: "open-page", pageId: node.id };
  }
  return { kind: "describe", nodeId: node.id };
}

export interface NodeActionDeps {
  /** Go to the Wiki view. */
  setView: (view: "wiki") => void;
  /** Open that page. */
  selectPage: (id: string | null) => void;
  /** Say what the node is. */
  describe: (nodeId: string) => void;
}

/**
 * Carry out what clicking a node means.
 *
 * Clicking a page node is navigation: the reader asked for that page,
 * so the app goes there and opens it (ADR-055). Clicking an entity or a
 * document says what it is - there is no page of its own to open.
 */
export function applyNodeAction(
  action: NodeAction,
  deps: NodeActionDeps
): void {
  if (action.kind === "open-page") {
    deps.setView("wiki");
    deps.selectPage(action.pageId);
    return;
  }
  deps.describe(action.nodeId);
}

// ===== the legend and the empty graph =====

export interface LegendEntry {
  type: string;
  label: string;
  color: string;
}

/** The kinds of node and edge this graph actually holds. */
export function legendEntries(graph: GraphData | null): {
  nodes: LegendEntry[];
  edges: LegendEntry[];
} {
  if (!graph) return { nodes: [], edges: [] };
  const nodeTypes = [...new Set(graph.nodes.map((n) => n.type))].sort((a, b) => {
    const rank = (t: string) =>
      t === "page" ? 0 : t === "document" ? 1 : 2;
    const diff = rank(a) - rank(b);
    return diff !== 0 ? diff : a < b ? -1 : a > b ? 1 : 0;
  });
  const edgeTypes = [...new Set(graph.edges.map((e) => e.type))].sort(
    (a, b) => {
      const order = [
        "derived_from",
        "references",
        "shares_evidence",
        "mentions",
        "co_occurs",
      ];
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      if (ia !== ib) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      return a < b ? -1 : a > b ? 1 : 0;
    }
  );
  return {
    nodes: nodeTypes.map((type) => ({
      type,
      label: nodeTypeLabel(type),
      color: nodeColor(type),
    })),
    edges: edgeTypes.map((type) => ({
      type,
      label: edgeTypeLabel(type),
      color: edgeColor(type),
    })),
  };
}

/**
 * What the graph has to say when it has nothing to show.
 *
 * A graph of scattered points with no edges is not a graph of
 * relations - it is a lie told with a picture. So the graph says so
 * (ADR-054).
 *
 * The edges it is given have to be the ones that are drawn: an edge
 * whose ends are not on the graph is not drawn, and a graph with no
 * drawn edge has to say so.
 */
export function graphNotice(
  graph: { nodes: unknown[]; edges: unknown[] } | null
): string | null {
  if (!graph) return null;
  if (graph.nodes.length === 0) return "图谱为空";
  if (graph.edges.length === 0) return "还没有关系";
  return null;
}
