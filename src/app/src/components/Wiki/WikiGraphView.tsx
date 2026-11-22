import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../../store";
import { getWikiGraph } from "../../api/backend";
import {
  applyNodeAction,
  computeLayout,
  describeNode,
  edgeColor,
  edgeTypeHint,
  graphNotice,
  legendEntries,
  nodeActionFor,
  withSavedPositions,
  VIEW_HEIGHT,
  VIEW_WIDTH,
  type GraphData,
  type Position,
} from "./graphLayout";

/**
 * The graph.
 *
 * It is an entrance, not a picture: clicking a page node opens that
 * page, clicking an entity or a document says what it is (ADR-055).
 * The layout is computed once per graph - the selection is not an
 * input to it, so clicking never moves anything (AC-F-034) - and the
 * place the reader dragged a node to is kept in the store.
 */

interface NodeShapeProps {
  id: string;
  label: string;
  type: string;
  x: number;
  y: number;
  radius: number;
  color: string;
  /** What the node is, in words - shown when the pointer rests on it. */
  hint: string;
  selected: boolean;
  onEnter: (id: string) => void;
  onLeave: (id: string) => void;
  onClick: (id: string, type: string) => void;
  onPointerDown: (
    event: React.PointerEvent<SVGGElement>,
    id: string,
    x: number,
    y: number
  ) => void;
}

const GraphNode = memo(function GraphNode(props: NodeShapeProps) {
  const { id, label, type, x, y, radius, color, hint, selected } = props;
  return (
    <g
      className="wiki-graph__node"
      transform={`translate(${x},${y})`}
      onPointerEnter={() => props.onEnter(id)}
      onPointerLeave={() => props.onLeave(id)}
      onClick={() => props.onClick(id, type)}
      onPointerDown={(event) => props.onPointerDown(event, id, x, y)}
    >
      <title>{hint ? `${label}｜${hint}` : label}</title>
      <circle
        r={selected ? radius + 3 : radius}
        fill={color}
        stroke={selected ? "#111827" : "#ffffff"}
        strokeWidth={selected ? 3 : 1.5}
      />
      <text
        x={radius + 6}
        y={4}
        fontSize={11}
        fill="currentColor"
        paintOrder="stroke"
        stroke="var(--bg-secondary)"
        strokeWidth={3}
        strokeLinejoin="round"
      >
        {label}
      </text>
    </g>
  );
});

export function WikiGraphView() {
  const graph = useAppStore((s) => s.wikiGraph);
  const setGraph = useAppStore((s) => s.setWikiGraph);
  const selectPage = useAppStore((s) => s.selectWikiPage);
  const setView = useAppStore((s) => s.setView);
  const selectedId = useAppStore((s) => s.selectedWikiPage);
  const savedPositions = useAppStore((s) => s.graphNodePositions);
  const setNodePosition = useAppStore((s) => s.setGraphNodePosition);

  const svgRef = useRef<SVGSVGElement | null>(null);
  const groupRef = useRef<SVGGElement | null>(null);
  /** The node the pointer last rested on. */
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  /** The node the reader clicked - an entity or a document. */
  const [pickedId, setPickedId] = useState<string | null>(null);
  /** The node being dragged right now. */
  const [drag, setDrag] = useState<{ id: string; x: number; y: number } | null>(
    null
  );
  const dragRef = useRef<{
    id: string;
    dx: number;
    dy: number;
    x: number;
    y: number;
    /** Where the node was when the pointer went down. */
    sx: number;
    sy: number;
  } | null>(null);
  /** Whether the pointer was dragging rather than clicking. */
  const movedRef = useRef(false);
  const panRef = useRef<{ x: number; y: number; tx: number; ty: number } | null>(
    null
  );
  const [viewport, setViewport] = useState({ x: 0, y: 0, k: 1 });

  useEffect(() => {
    if (!graph) {
      getWikiGraph()
        .then(setGraph)
        .catch((e) => console.error("graph fetch failed", e));
    }
  }, [graph, setGraph]);

  // The layout is computed once per graph. The selection is not an
  // input to it - clicking a node must not move anything (AC-F-034).
  const baseLayout = useMemo(
    () => (graph ? computeLayout(graph) : null),
    [graph]
  );

  // The place the reader dragged the nodes to wins over the computed
  // one; the node being dragged right now wins over both.
  const layout = useMemo(() => {
    if (!baseLayout) return null;
    const placed = withSavedPositions(baseLayout, savedPositions);
    if (!drag) return placed;
    return withSavedPositions(placed, { [drag.id]: { x: drag.x, y: drag.y } });
  }, [baseLayout, savedPositions, drag]);

  const byId = useMemo(
    () => new Map((layout?.nodes ?? []).map((n) => [n.id, n])),
    [layout]
  );

  const pageLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    for (const node of graph?.nodes ?? []) {
      if (node.type === "page") labels[node.id] = node.label;
    }
    return labels;
  }, [graph]);

  // The legend and the notice have to be about the edges that are
  // really drawn: an edge whose ends are not on the graph is not
  // drawn, and a graph with no drawn edge has to say so (AC-F-035).
  // The saved positions do not change the edges, so the drawn ones
  // are taken from the base layout - they do not move while a node
  // is being dragged.
  const drawn: GraphData = useMemo(
    () => ({
      nodes: baseLayout?.nodes ?? [],
      edges: baseLayout?.edges ?? [],
    }),
    [baseLayout]
  );

  const legend = useMemo(() => legendEntries(drawn), [drawn]);
  const notice = useMemo(() => graphNotice(drawn), [drawn]);

  /** What each node says about itself - shown without opening it. */
  const hints = useMemo(() => {
    const map: Record<string, string> = {};
    for (const node of layout?.nodes ?? []) {
      map[node.id] = describeNode(node, pageLabels).lines.join("；");
    }
    return map;
  }, [layout, pageLabels]);

  const activeId = hoveredId ?? pickedId;
  const description = useMemo(() => {
    if (!activeId) return null;
    const node = byId.get(activeId);
    if (!node) return null;
    return describeNode(node, pageLabels);
  }, [activeId, byId, pageLabels]);

  /**
   * The pointer left a node.
   *
   * The panel about the node goes with it: a panel that stays after
   * the pointer has gone covers the drawing and says nothing. The
   * check keeps a node that was just entered from being cleared by
   * the leave of the node it came from.
   */
  const clearHovered = useCallback((id: string) => {
    setHoveredId((current) => (current === id ? null : current));
  }, []);

  /** A point of the screen in the coordinates of the drawing. */
  const toLocal = useCallback(
    (clientX: number, clientY: number): Position => {
      const group = groupRef.current;
      if (!group) return { x: 0, y: 0 };
      const ctm = group.getScreenCTM();
      if (!ctm) return { x: 0, y: 0 };
      const point = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
      return { x: point.x, y: point.y };
    },
    []
  );

  /** A point of the screen in the coordinates of the canvas. */
  const toBase = useCallback(
    (clientX: number, clientY: number): Position => {
      const svg = svgRef.current;
      if (!svg) return { x: 0, y: 0 };
      const ctm = svg.getScreenCTM();
      if (!ctm) return { x: 0, y: 0 };
      const point = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
      return { x: point.x, y: point.y };
    },
    []
  );

  // The wheel zooms. It is a native listener so that it can stop the
  // page from scrolling under it.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      const at = toLocal(event.clientX, event.clientY);
      setViewport((t) => {
        const k = Math.min(5, Math.max(0.2, t.k * factor));
        return { k, x: t.x + (t.k - k) * at.x, y: t.y + (t.k - k) * at.y };
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [toLocal]);

  const onNodePointerDown = useCallback(
    (
      event: React.PointerEvent<SVGGElement>,
      id: string,
      x: number,
      y: number
    ) => {
      event.stopPropagation();
      movedRef.current = false;
      const at = toLocal(event.clientX, event.clientY);
      dragRef.current = {
        id,
        dx: at.x - x,
        dy: at.y - y,
        x,
        y,
        sx: x,
        sy: y,
      };
      setDrag({ id, x, y });
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [toLocal]
  );

  const onBackgroundPointerDown = useCallback(
    (event: React.PointerEvent<SVGSVGElement>) => {
      if (event.button !== 0) return;
      const at = toBase(event.clientX, event.clientY);
      panRef.current = { x: at.x, y: at.y, tx: viewport.x, ty: viewport.y };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [toBase, viewport.x, viewport.y]
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<SVGSVGElement>) => {
      const d = dragRef.current;
      if (d) {
        const at = toLocal(event.clientX, event.clientY);
        const x = at.x - d.dx;
        const y = at.y - d.dy;
        // A hand that shakes is not a drag.
        if (Math.hypot(x - d.sx, y - d.sy) > 3) movedRef.current = true;
        dragRef.current = { ...d, x, y };
        setDrag({ id: d.id, x, y });
        return;
      }
      const p = panRef.current;
      if (p) {
        const at = toBase(event.clientX, event.clientY);
        setViewport((t) => ({
          ...t,
          x: p.tx + (at.x - p.x),
          y: p.ty + (at.y - p.y),
        }));
      }
    },
    [toLocal, toBase]
  );

  const endDrag = useCallback(() => {
    const d = dragRef.current;
    if (d) {
      // The place the reader put the node is remembered - but only if
      // they really moved it. A click does not pin a node.
      if (movedRef.current) setNodePosition(d.id, { x: d.x, y: d.y });
      dragRef.current = null;
      setDrag(null);
    }
    panRef.current = null;
  }, [setNodePosition]);

  const onNodeClick = useCallback(
    (id: string, type: string) => {
      if (movedRef.current) {
        // The pointer was dragging the node, not clicking it.
        movedRef.current = false;
        return;
      }
      const action = nodeActionFor({ id, type });
      applyNodeAction(action, {
        setView,
        selectPage,
        describe: (nodeId) => setPickedId(nodeId),
      });
    },
    [setView, selectPage]
  );

  const refresh = () => {
    setGraph(null);
    getWikiGraph()
      .then(setGraph)
      .catch((e) => console.error("graph fetch failed", e));
  };

  return (
    <div className="wiki-graph">
      <div className="wiki-graph__header">
        <span className="wiki-graph__title">实体图谱</span>
        <div className="wiki-graph__legend">
          {legend.nodes.map((entry) => (
            <span key={`node-${entry.type}`} className="wiki-graph__legend-item">
              <span
                className="wiki-graph__dot"
                style={{ background: entry.color }}
              />
              {entry.label}
            </span>
          ))}
          {legend.edges.map((entry) => (
            <span key={`edge-${entry.type}`} className="wiki-graph__legend-item">
              <span
                className="wiki-graph__line"
                style={{ background: entry.color }}
              />
              {entry.label}
            </span>
          ))}
        </div>
        <button className="btn" onClick={refresh}>
          ↻ 刷新
        </button>
      </div>
      {graph && graph.nodes.length === 0 ? (
        <div className="empty-state">图谱为空</div>
      ) : !graph ? (
        <div className="empty-state">加载中…</div>
      ) : (
        <div className="wiki-graph__canvas">
          <svg
            ref={svgRef}
            className="wiki-graph__svg"
            viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
            preserveAspectRatio="xMidYMid meet"
            onPointerDown={onBackgroundPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          >
            <g
              ref={groupRef}
              transform={`translate(${viewport.x},${viewport.y}) scale(${viewport.k})`}
            >
              {(layout?.edges ?? []).map((edge) => {
                const from = byId.get(edge.source);
                const to = byId.get(edge.target);
                if (!from || !to) return null;
                return (
                  <line
                    key={`${edge.source}-${edge.target}-${edge.type}`}
                    x1={from.x}
                    y1={from.y}
                    x2={to.x}
                    y2={to.y}
                    stroke={edgeColor(edge.type)}
                    strokeWidth={Math.max(1, Math.min(3, edge.weight))}
                    strokeOpacity={0.55}
                  >
                    <title>{edgeTypeHint(edge.type)}</title>
                  </line>
                );
              })}
              {(layout?.nodes ?? []).map((node) => (
                <GraphNode
                  key={node.id}
                  id={node.id}
                  label={node.label}
                  type={node.type}
                  x={node.x}
                  y={node.y}
                  radius={node.radius}
                  color={node.color}
                  hint={hints[node.id] ?? ""}
                  selected={node.id === selectedId || node.id === pickedId}
                  onEnter={setHoveredId}
                  onLeave={clearHovered}
                  onClick={onNodeClick}
                  onPointerDown={onNodePointerDown}
                />
              ))}
            </g>
          </svg>
          {notice && <div className="wiki-graph__notice">{notice}</div>}
          {description && (
            <div className="wiki-graph__detail">
              <div className="wiki-graph__detail-title">{description.title}</div>
              <div className="wiki-graph__detail-kind">{description.kind}</div>
              <ul className="wiki-graph__detail-list">
                {description.lines.map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      <div className="wiki-graph__hint">
        拖动节点 / 滚轮缩放 / 点击页面节点跳转 / 点实体或文件节点看它是什么
      </div>
      <style>{graphStyles}</style>
    </div>
  );
}

const graphStyles = `
  .wiki-graph { display: flex; flex-direction: column; height: 100%; }
  .wiki-graph__header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    flex-wrap: wrap;
  }
  .wiki-graph__title { font-weight: 600; }
  .wiki-graph__legend {
    display: flex;
    gap: 12px;
    flex: 1;
    flex-wrap: wrap;
    font-weight: 400;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .wiki-graph__legend-item {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .wiki-graph__dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
  }
  .wiki-graph__line {
    width: 14px;
    height: 2px;
    display: inline-block;
  }
  .wiki-graph__canvas { position: relative; flex: 1; min-height: 0; }
  .wiki-graph__svg {
    width: 100%;
    height: 100%;
    background: var(--bg-secondary);
    display: block;
    touch-action: none;
  }
  .wiki-graph__node { cursor: pointer; }
  .wiki-graph__notice {
    position: absolute;
    top: 16px;
    left: 50%;
    transform: translateX(-50%);
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 13px;
    color: var(--text-primary);
    background: var(--bg-primary, #fff);
    border: 1px solid var(--border);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
    pointer-events: none;
  }
  .wiki-graph__detail {
    position: absolute;
    left: 16px;
    bottom: 16px;
    max-width: 360px;
    /* The panel is a read-only note over the drawing: it must never
       take the pointer, or the nodes under it can neither be clicked
       nor dragged - and the pointer would leave the node as soon as
       it reached the panel, so the panel would blink. */
    pointer-events: none;
    /* A file that made twenty pages says twenty titles; the panel
       has to stay a note and not cover the drawing. */
    max-height: 40vh;
    overflow: auto;
    padding: 10px 12px;
    border-radius: 8px;
    font-size: 12px;
    color: var(--text-primary);
    background: var(--bg-primary, #fff);
    border: 1px solid var(--border);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  }
  .wiki-graph__detail-title { font-weight: 600; }
  .wiki-graph__detail-kind {
    color: var(--text-secondary);
    margin-bottom: 4px;
  }
  .wiki-graph__detail-list {
    margin: 0;
    padding-left: 16px;
    line-height: 1.5;
  }
  .wiki-graph__hint {
    padding: 6px 16px;
    font-size: 11px;
    color: var(--text-tertiary);
    border-top: 1px solid var(--border);
    text-align: center;
  }
`;
