/**
 * The graph: relations, and clicking a node.
 *
 * The user reported two things: 「图谱有什么作用」and「点击蓝色的只是刷新一次」.
 * Both were true of the old view. It drew 136 nodes and no edges at
 * all, clicking a page node only wrote an id into the store (the view
 * did not move — it looked like a refresh), and the selection was an
 * input to the layout, so every click rebuilt the drawing and the node
 * the reader had just clicked ran away (ADR-054, ADR-055).
 *
 * What this file pins:
 *
 * - **the layout is a pure function** — the same graph always gives the
 *   same positions, and the selection is not an input to it (AC-F-034);
 * - **the relations are sayable** — every edge kind has a name, and a
 *   graph without edges says 「还没有关系」rather than drawing a lie;
 * - **clicking is navigation** — a page node opens that page, an entity
 *   or a document says what it is;
 * - **the positions are kept** — a node the reader dragged stays where
 *   they put it, even after going to another view and back.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  EDGE_TYPE_LABELS,
  applyNodeAction,
  computeLayout,
  describeNode,
  edgeTypeHint,
  edgeTypeLabel,
  graphNotice,
  layoutGraph,
  legendEntries,
  nodeActionFor,
  nodeTypeLabel,
  withSavedPositions,
  type GraphData,
} from "../src/components/Wiki/graphLayout.ts";

import { useAppStore } from "../src/store.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]) =>
  readFileSync(join(here, "..", ...parts), "utf-8");

const graphSource = read("src", "components", "Wiki", "WikiGraphView.tsx");
const layoutSource = read("src", "components", "Wiki", "graphLayout.ts");
const storeSource = read("src", "store.ts");

/** A graph shaped like the real one: pages, a document, an entity. */
const GRAPH: GraphData = {
  nodes: [
    {
      id: "p1",
      label: "华润置地",
      type: "page",
      weight: 1,
      key_points: ["要点一：土地储备", "要点二：商业运营"],
      related_pages: ["p2"],
    },
    {
      id: "p2",
      label: "华润万象",
      type: "page",
      weight: 1,
      key_points: ["要点一：购物中心"],
      related_pages: ["p1"],
    },
    {
      id: "doc:d1",
      label: "2024年报.pdf",
      type: "document",
      weight: 2,
      category: "pdf",
      related_pages: ["p1", "p2"],
    },
    {
      id: "entity:e1",
      label: "华润",
      type: "entity_org",
      weight: 3,
      aliases: ["华润集团"],
      related_pages: ["p1"],
    },
  ],
  edges: [
    { source: "p1", target: "doc:d1", type: "derived_from", weight: 1 },
    { source: "p2", target: "doc:d1", type: "derived_from", weight: 1 },
    { source: "p1", target: "entity:e1", type: "mentions", weight: 1 },
  ],
};

const PAGE_LABELS = { p1: "华润置地", p2: "华润万象" };

/** The positions of a layout, as something two layouts can be compared on. */
function positions(layout: { nodes: { id: string; x: number; y: number }[] }) {
  return layout.nodes
    .slice()
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((n) => [n.id, n.x, n.y]);
}

// ===== the layout is a pure function =====

test("the same graph always gives the same positions", () => {
  const first = layoutGraph(GRAPH);
  const second = layoutGraph(GRAPH);
  assert.deepEqual(positions(first), positions(second));
  assert.ok(
    first.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)),
    "every node has a real place"
  );
});

test("the order of the nodes and the edges does not matter", () => {
  const shuffled: GraphData = {
    nodes: [...GRAPH.nodes].reverse(),
    edges: [...GRAPH.edges].reverse(),
  };
  assert.deepEqual(positions(layoutGraph(shuffled)), positions(layoutGraph(GRAPH)));
});

test("the selection is not an input to the layout", () => {
  // No function of the layout takes a selection: if one did, the
  // positions would move when the reader clicked (AC-F-034).
  const signatures = [
    ...layoutSource.matchAll(/export function (\w+)\(([^)]*)\)/g),
  ];
  assert.ok(signatures.length > 0, "the layout has functions");
  for (const [, name, params] of signatures) {
    assert.doesNotMatch(params, /select/i, `${name} takes a selection`);
  }
  assert.doesNotMatch(layoutSource, /selectedWikiPage/);

  // And the drawing is not rebuilt when the selection changes: the
  // layout is computed once per graph, from the graph alone.
  const memo = graphSource.match(
    /const baseLayout = useMemo\([\s\S]*?\n\s*\);/
  );
  assert.ok(memo, "the layout is a memo of its own");
  assert.match(memo[0], /\[graph\]/);
  assert.doesNotMatch(memo[0], /selected/i);

  // No force simulation is started at all — the old one was rebuilt
  // on every click, and the node the reader clicked ran away.
  assert.doesNotMatch(graphSource, /forceSimulation/);
  assert.doesNotMatch(graphSource, /from "d3"/);
});

test("the layout leaves the graph on the page and keeps the nodes apart", () => {
  const layout = layoutGraph(GRAPH);
  for (const node of layout.nodes) {
    assert.ok(node.x > 0 && node.x < layout.width, `${node.id} is on the page`);
    assert.ok(node.y > 0 && node.y < layout.height, `${node.id} is on the page`);
  }
  // Two nodes on the same spot are one node the reader cannot tell
  // from the other.
  for (let i = 0; i < layout.nodes.length; i++) {
    for (let j = i + 1; j < layout.nodes.length; j++) {
      const a = layout.nodes[i];
      const b = layout.nodes[j];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      assert.ok(d > 10, `${a.id} and ${b.id} sit on top of each other`);
    }
  }
});

test("a node the reader dragged keeps the place they put it", () => {
  const base = layoutGraph(GRAPH);
  const placed = layoutGraph(GRAPH, { p1: { x: 111, y: 222 } });
  const moved = placed.nodes.find((n) => n.id === "p1");
  assert.ok(moved);
  assert.equal(moved.x, 111);
  assert.equal(moved.y, 222);

  // The others do not move: dragging one node must not shuffle the
  // rest of the graph.
  for (const node of base.nodes) {
    if (node.id === "p1") continue;
    const other = placed.nodes.find((n) => n.id === node.id);
    assert.ok(other);
    assert.equal(other.x, node.x);
    assert.equal(other.y, node.y);
  }
});

test("the dragged positions are kept in the store", () => {
  const store = useAppStore.getState();
  store.setGraphNodePosition("p1", { x: 12, y: 34 });

  // The reader goes to another view and comes back.
  store.setView("chat");
  store.setView("graph");

  const state = useAppStore.getState();
  assert.deepEqual(state.graphNodePositions["p1"], { x: 12, y: 34 });
  assert.equal(state.view, "graph");

  // And the layout puts the node there.
  const laid = withSavedPositions(layoutGraph(GRAPH), state.graphNodePositions);
  const node = laid.nodes.find((n) => n.id === "p1");
  assert.ok(node);
  assert.equal(node.x, 12);
  assert.equal(node.y, 34);

  useAppStore.setState({ graphNodePositions: {} });
});

test("the drawing reads the places from the store and puts the node there", () => {
  // The place the reader dragged a node to is kept in the store
  // (ADR-055), and the drawing has to take it from there - a copy
  // of its own would be thrown away when the reader goes to another
  // view, and the graph would shuffle under them.
  assert.match(
    graphSource,
    /useAppStore\(\(s\) => s\.graphNodePositions\)/,
    "the drawing reads the places from the store"
  );
  assert.match(
    graphSource,
    /withSavedPositions\(baseLayout, savedPositions\)/,
    "the drawing puts the node where the store says"
  );
  assert.match(
    graphSource,
    /\[baseLayout, savedPositions, drag\]/,
    "the drawing is redrawn when the place changes"
  );

  // And the numbers the store holds are the numbers the node gets.
  const store = useAppStore.getState();
  store.setGraphNodePosition("p1", { x: 12, y: 34 });
  const state = useAppStore.getState();
  const placed = withSavedPositions(
    computeLayout(GRAPH),
    state.graphNodePositions
  );
  const node = placed.nodes.find((n) => n.id === "p1");
  assert.ok(node);
  assert.equal(node.x, 12);
  assert.equal(node.y, 34);

  useAppStore.setState({ graphNodePositions: {} });
});

test("the store keeps one place per node", () => {
  const store = useAppStore.getState();
  store.setGraphNodePosition("p1", { x: 1, y: 2 });
  store.setGraphNodePosition("p2", { x: 3, y: 4 });
  let state = useAppStore.getState();
  assert.equal(Object.keys(state.graphNodePositions).length, 2);
  assert.deepEqual(state.graphNodePositions["p2"], { x: 3, y: 4 });

  store.setGraphNodePosition("p2", { x: 5, y: 6 });
  state = useAppStore.getState();
  assert.equal(Object.keys(state.graphNodePositions).length, 2);
  assert.deepEqual(state.graphNodePositions["p2"], { x: 5, y: 6 });

  useAppStore.setState({ graphNodePositions: {} });
});

// The layout of a graph the size of a real one: the reader has to be
// able to tell the nodes apart, and the drawing has to be quick.
test("a real sized graph is drawn in one go and the nodes can be told apart", () => {
  const nodes: GraphData["nodes"] = [];
  const edges: GraphData["edges"] = [];
  for (let i = 0; i < 36; i++)
    nodes.push({ id: `p${i}`, label: `页面 ${i}`, type: "page" });
  for (let i = 0; i < 26; i++)
    nodes.push({ id: `doc:d${i}`, label: `文件 ${i}.pdf`, type: "document" });
  for (let i = 0; i < 100; i++)
    nodes.push({
      id: `entity:e${i}`,
      label: `实体 ${i}`,
      type: `entity_${["person", "concept", "project", "contract", "law"][i % 5]}`,
    });
  for (let i = 0; i < 63; i++)
    edges.push({
      source: `p${i % 36}`,
      target: `doc:d${i % 26}`,
      type: "derived_from",
    });
  for (let i = 0; i < 49; i++)
    edges.push({
      source: `p${(i * 3) % 36}`,
      target: `entity:e${i}`,
      type: "mentions",
    });
  for (let i = 0; i < 48; i++)
    edges.push({
      source: `entity:e${i}`,
      target: `entity:e${(i * 7 + 3) % 100}`,
      type: "co_occurs",
    });
  for (let i = 0; i < 9; i++)
    edges.push({
      source: `p${i}`,
      target: `p${(i * 5 + 1) % 36}`,
      type: "shares_evidence",
    });

  const started = Date.now();
  const layout = computeLayout({ nodes, edges });
  const elapsed = Date.now() - started;

  assert.equal(layout.nodes.length, 162);
  assert.equal(layout.edges.length, 169);
  assert.ok(elapsed < 2000, `the drawing took ${elapsed} ms`);

  // Every node is on the page, and no two of them sit on one spot.
  for (const node of layout.nodes) {
    assert.ok(node.x >= 0 && node.x <= layout.width);
    assert.ok(node.y >= 0 && node.y <= layout.height);
  }
  for (let i = 0; i < layout.nodes.length; i++) {
    for (let j = i + 1; j < layout.nodes.length; j++) {
      const a = layout.nodes[i];
      const b = layout.nodes[j];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      assert.ok(d > 12, `${a.id} and ${b.id} sit on top of each other`);
    }
  }
});

// The biggest graph the backend allows: 500 pages, 100 entities and
// the files they came from. The layout is worked out on the main
// thread when the reader opens the graph, so the worst case has to
// be quick too - the graph is an entrance, not a place to wait.
test("the biggest graph the backend allows is drawn in one go", () => {
  const nodes: GraphData["nodes"] = [];
  const edges: GraphData["edges"] = [];
  for (let i = 0; i < 500; i++)
    nodes.push({ id: `p${i}`, label: `页面 ${i}`, type: "page" });
  for (let i = 0; i < 26; i++)
    nodes.push({ id: `doc:d${i}`, label: `文件 ${i}.pdf`, type: "document" });
  for (let i = 0; i < 100; i++)
    nodes.push({
      id: `entity:e${i}`,
      label: `实体 ${i}`,
      type: `entity_${["person", "concept", "project", "contract", "law"][i % 5]}`,
    });
  for (let i = 0; i < 500; i++)
    edges.push({
      source: `p${i}`,
      target: `doc:d${i % 26}`,
      type: "derived_from",
    });
  for (let i = 0; i < 400; i++)
    edges.push({
      source: `p${(i * 7) % 500}`,
      target: `entity:e${i % 100}`,
      type: "mentions",
    });
  for (let i = 0; i < 100; i++)
    edges.push({
      source: `entity:e${i}`,
      target: `entity:e${(i * 3 + 1) % 100}`,
      type: "co_occurs",
    });
  for (let i = 0; i < 60; i++)
    edges.push({
      source: `p${i}`,
      target: `p${(i * 11 + 3) % 500}`,
      type: "shares_evidence",
    });

  const started = Date.now();
  const layout = computeLayout({ nodes, edges });
  const elapsed = Date.now() - started;

  assert.equal(layout.nodes.length, 626);
  // Every edge is drawn once: the drawing does not draw the same
  // relation twice.
  assert.equal(
    layout.edges.length,
    new Set(edges.map((e) => `${e.source} ${e.target} ${e.type}`)).size
  );
  assert.ok(elapsed < 2000, `the biggest graph took ${elapsed} ms`);

  // Every node is on the page.
  for (const node of layout.nodes) {
    assert.ok(node.x >= 0 && node.x <= layout.width);
    assert.ok(node.y >= 0 && node.y <= layout.height);
  }

  // The nodes can still be told apart: six hundred of them on one
  // canvas is a crowd, but no two of them sit on one spot.
  let closest = Infinity;
  for (let i = 0; i < layout.nodes.length; i++) {
    for (let j = i + 1; j < layout.nodes.length; j++) {
      const a = layout.nodes[i];
      const b = layout.nodes[j];
      closest = Math.min(closest, Math.hypot(a.x - b.x, a.y - b.y));
    }
  }
  assert.ok(closest > 10, `the closest two nodes are ${closest} apart`);
});

// ===== clicking a node =====

test("clicking a page node opens that page", () => {
  const calls: string[] = [];
  const action = nodeActionFor({ id: "p1", type: "page" });
  assert.deepEqual(action, { kind: "open-page", pageId: "p1" });

  applyNodeAction(action, {
    setView: (v) => calls.push(`view:${v}`),
    selectPage: (id) => calls.push(`page:${id}`),
    describe: (id) => calls.push(`describe:${id}`),
  });

  // The graph is an entrance: the page has to come out (ADR-055).
  assert.deepEqual(calls, ["view:wiki", "page:p1"]);
});

test("the drawing carries the click out with the store's own view and page", () => {
  // The drawing has to hand the click over - otherwise 「点击蓝色的
  // 只是刷新一次」happens again: the id goes into the store and the
  // app stays where it was (AC-F-034).
  const click = graphSource.match(
    /const onNodeClick = useCallback\([\s\S]*?\n  \);/
  );
  assert.ok(click, "the drawing has a click handler");
  assert.match(click[0], /nodeActionFor\(/);

  const deps = click[0].match(/applyNodeAction\(action, \{([\s\S]*?)\}\)/);
  assert.ok(deps, "the click is carried out");
  assert.match(deps[1], /setView/, "the click moves the app to the wiki");
  assert.match(deps[1], /selectPage/, "the click opens the page");

  // And both of them are the store's own - not a copy of its own.
  assert.match(graphSource, /useAppStore\(\(s\) => s\.setView\)/);
  assert.match(graphSource, /useAppStore\(\(s\) => s\.selectWikiPage\)/);

  // The store really does move the app: the view goes to the wiki
  // and the page that was clicked is the one that is open.
  const store = useAppStore.getState();
  store.setView("wiki");
  store.selectWikiPage("p1");
  const state = useAppStore.getState();
  assert.equal(state.view, "wiki");
  assert.equal(state.selectedWikiPage, "p1");

  useAppStore.setState({ view: "chat", selectedWikiPage: null });
});

test("the note over the drawing does not take the pointer", () => {
  // The panel is a read-only note over the drawing. If it took the
  // pointer, the nodes under it could neither be clicked nor
  // dragged - and the pointer leaving the node for the panel would
  // make the panel blink in and out.
  const panel = graphSource.match(/\.wiki-graph__detail \{[^}]*\}/);
  assert.ok(panel, "the panel has a style of its own");
  assert.match(panel[0], /pointer-events: none/);
  assert.match(panel[0], /max-height/);

  // And the pointer leaving a node takes the panel with it: a panel
  // that stays after the pointer has gone covers the drawing and
  // says nothing.
  assert.match(
    graphSource,
    /onPointerLeave=\{\(\) => props\.onLeave\(id\)\}/,
    "the node says when the pointer has gone"
  );
  assert.match(
    graphSource,
    /onLeave=\{clearHovered\}/,
    "the panel is taken down with it"
  );
});

test("clicking an entity or a document says what it is", () => {
  for (const node of [
    { id: "entity:e1", type: "entity_org" },
    { id: "doc:d1", type: "document" },
  ]) {
    const calls: string[] = [];
    const action = nodeActionFor(node);
    assert.deepEqual(action, { kind: "describe", nodeId: node.id });
    applyNodeAction(action, {
      setView: (v) => calls.push(`view:${v}`),
      selectPage: (id) => calls.push(`page:${id}`),
      describe: (id) => calls.push(`describe:${id}`),
    });
    // There is no page of its own to open, so nothing navigates.
    assert.deepEqual(calls, [`describe:${node.id}`]);
  }
});

test("an entity says its name, its kind, its other names and its pages", () => {
  const node = GRAPH.nodes.find((n) => n.id === "entity:e1");
  assert.ok(node);
  const described = describeNode(node, PAGE_LABELS);

  assert.equal(described.title, "华润");
  assert.match(described.kind, /实体/);
  assert.match(described.kind, /机构/);
  assert.ok(
    described.lines.some((l) => l.includes("华润集团")),
    "the other names are said"
  );
  assert.ok(
    described.lines.some((l) => l.includes("华润置地")),
    "the pages that mention it are said"
  );
});

test("a document says its file name and the pages compiled from it", () => {
  const node = GRAPH.nodes.find((n) => n.id === "doc:d1");
  assert.ok(node);
  const described = describeNode(node, PAGE_LABELS);

  assert.equal(described.title, "2024年报.pdf");
  assert.match(described.kind, /源文件/);
  const line = described.lines.find((l) => l.includes("华润置地"));
  assert.ok(line, "the pages it made are said");
  assert.match(line, /华润万象/);
});

test("the file type is said in the reader's words", () => {
  // The backend sends the suffix ("pdf", "md"); a Chinese panel should
  // not say "markdown" in the middle of a Chinese sentence.
  const node = {
    id: "doc:d2",
    label: "采购合同.md",
    type: "document",
    weight: 1,
    category: "md",
    related_pages: [],
  };
  const described = describeNode(node, PAGE_LABELS);

  const line = described.lines.find((l) => l.includes("文件类型"));
  assert.ok(line, "the file type is said");
  assert.match(line, /Markdown/);
  assert.ok(
    !/\bmd\b/.test(line.replace("Markdown", "")),
    `the raw suffix is still showing: ${line}`
  );
});

test("the key points of a page are visible without opening the page", () => {
  const node = GRAPH.nodes.find((n) => n.id === "p1");
  assert.ok(node);
  const described = describeNode(node, PAGE_LABELS);

  assert.match(described.kind, /页面/);
  assert.ok(
    described.lines.some((l) => l.includes("土地储备")),
    "the key points are said"
  );
  assert.ok(
    described.lines.some((l) => l.includes("商业运营")),
    "the key points are said"
  );

  // And the drawing shows them on the node itself, not only in the
  // Wiki view.
  assert.match(graphSource, /describeNode/);
  assert.match(graphSource, /description\.lines/);
});

// ===== the legend and the graph with no relations =====

test("every kind of edge has a name the reader can read", () => {
  for (const type of [
    "derived_from",
    "references",
    "shares_evidence",
    "mentions",
    "co_occurs",
  ]) {
    assert.ok(EDGE_TYPE_LABELS[type], `${type} has no name`);
    assert.match(EDGE_TYPE_LABELS[type], /[一-鿿]/, `${type} is not in words`);
  }
});

test("the legend says what the edges are", () => {
  const legend = legendEntries(GRAPH);
  const types = legend.edges.map((e) => e.type);
  assert.ok(types.includes("derived_from"));
  assert.ok(types.includes("mentions"));
  for (const entry of legend.edges) {
    assert.equal(entry.label, EDGE_TYPE_LABELS[entry.type]);
    assert.ok(entry.color);
  }

  // The node kinds are in the legend too.
  const nodeTypes = legend.nodes.map((e) => e.type);
  assert.ok(nodeTypes.includes("page"));
  assert.ok(nodeTypes.includes("document"));
  assert.ok(nodeTypes.includes("entity_org"));

  // And the drawing shows them.
  assert.match(graphSource, /legend\.edges\.map/);
  assert.match(graphSource, /legend\.nodes\.map/);
});

test("an edge kind nobody knows is named, not waved away", () => {
  // Every edge has to be sayable (AC-F-035). A kind the drawing does
  // not know must not be waved away as 「有关系」- that would look
  // like an explanation and say nothing. The unknown kind is named,
  // so the reader can see that there is a relation here which has
  // not been worked out yet.
  const label = edgeTypeLabel("from_the_future");
  assert.notEqual(label, "有关系");
  assert.match(label, /from_the_future/);
  assert.notEqual(edgeTypeHint("from_the_future"), "一条有依据的关系");

  // And it is in the legend, so the reader can see it there.
  const legend = legendEntries({
    nodes: GRAPH.nodes,
    edges: [
      ...GRAPH.edges,
      { source: "p1", target: "p2", type: "from_the_future" },
    ],
  });
  const entry = legend.edges.find((e) => e.type === "from_the_future");
  assert.ok(entry, "the unknown kind is in the legend");
  assert.equal(entry.label, label);
});

test("a graph without relations says so", () => {
  assert.equal(graphNotice(GRAPH), null);
  assert.equal(
    graphNotice({ nodes: GRAPH.nodes, edges: [] }),
    "还没有关系"
  );
  assert.equal(graphNotice({ nodes: [], edges: [] }), "图谱为空");

  // And the drawing says it too: the wording lives with the graph's
  // own logic, and the drawing shows whatever it says.
  assert.match(layoutSource, /还没有关系/);
  assert.match(graphSource, /graphNotice\(drawn\)/);
  assert.match(graphSource, /wiki-graph__notice/);
});

test("the notice is about the edges that are drawn", () => {
  // An edge whose ends are not on the graph is not drawn. A graph
  // of such edges has no relation on it, and it has to say so
  // rather than stay quiet and show a heap of points (AC-F-035).
  const dangling = {
    nodes: GRAPH.nodes,
    edges: [{ source: "p1", target: "nowhere", type: "mentions" }],
  };
  const laid = computeLayout(dangling);
  assert.equal(laid.edges.length, 0, "the edge is not drawn");
  assert.equal(
    graphNotice({ nodes: laid.nodes, edges: laid.edges }),
    "还没有关系"
  );

  // And the drawing judges the same edges it draws.
  assert.match(graphSource, /edges: baseLayout\?\.edges \?\? \[\]/);
});

test("the names of the kinds are in words the reader thinks in", () => {
  assert.equal(nodeTypeLabel("page"), "页面");
  assert.equal(nodeTypeLabel("document"), "源文件");
  assert.equal(nodeTypeLabel("entity_person"), "人物");
  assert.equal(nodeTypeLabel("entity_concept"), "概念");
  assert.equal(nodeTypeLabel("entity_project"), "项目");
  assert.equal(nodeTypeLabel("entity_contract"), "合同");
  assert.equal(nodeTypeLabel("entity_law"), "法规");
});
