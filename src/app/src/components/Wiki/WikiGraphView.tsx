import { useEffect, useRef } from "react";
import * as d3 from "d3";
import { useAppStore } from "../../store";
import { getWikiGraph } from "../../api/backend";

interface SimNode extends d3.SimulationNodeDatum {
  id: string;
  label: string;
  type: string;
  category?: string;
  weight: number;
}

interface SimLink extends d3.SimulationLinkDatum<SimNode> {
  type: string;
  weight: number;
}

const TYPE_COLORS: Record<string, string> = {
  page: "#2563eb",
  entity_person: "#16a34a",
  entity_concept: "#9333ea",
  entity_project: "#f59e0b",
  entity_contract: "#dc2626",
  entity_law: "#0891b2",
};

const TYPE_RADIUS: Record<string, number> = {
  page: 14,
  entity_person: 9,
  entity_concept: 9,
  entity_project: 11,
  entity_contract: 11,
  entity_law: 11,
};

export function WikiGraphView() {
  const ref = useRef<SVGSVGElement | null>(null);
  const graph = useAppStore((s) => s.wikiGraph);
  const setGraph = useAppStore((s) => s.setWikiGraph);
  const selectPage = useAppStore((s) => s.selectWikiPage);
  const selectedId = useAppStore((s) => s.selectedWikiPage);

  useEffect(() => {
    if (!graph) {
      getWikiGraph()
        .then(setGraph)
        .catch((e) => console.error("graph fetch failed", e));
    }
  }, [graph, setGraph]);

  useEffect(() => {
    if (!graph || !ref.current) return;
    const svg = d3.select(ref.current);
    svg.selectAll("*").remove();

    const width = ref.current.clientWidth || 800;
    const height = ref.current.clientHeight || 600;

    const nodes: SimNode[] = graph.nodes.map((n) => ({
      id: n.id,
      label: n.label,
      type: n.type,
      category: n.category,
      weight: n.weight,
    }));
    const links: SimLink[] = graph.edges.map((e) => ({
      source: e.source,
      target: e.target,
      type: e.type,
      weight: e.weight,
    }));

    // Group container so zoom transforms apply to both nodes and links.
    const root = svg.append("g");

    const link = root
      .append("g")
      .attr("stroke", "#a0a0a0")
      .attr("stroke-opacity", 0.6)
      .selectAll<SVGLineElement, SimLink>("line")
      .data(links)
      .join("line")
      .attr("stroke-width", (d) => Math.max(1, Math.min(3, d.weight)));

    const node = root
      .append("g")
      .selectAll<SVGGElement, SimNode>("g")
      .data(nodes, (d) => d.id)
      .join("g")
      .style("cursor", (d) => (d.type === "page" ? "pointer" : "default"))
      .on("click", (_event, d) => {
        if (d.type === "page") selectPage(d.id);
      })
      .call(
        d3
          .drag<SVGGElement, SimNode>()
          .on("start", (event, d) => {
            if (!event.active) sim.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (event, d) => {
            d.fx = event.x;
            d.fy = event.y;
          })
          .on("end", (event, d) => {
            if (!event.active) sim.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      );

    node
      .append("circle")
      .attr("r", (d) => TYPE_RADIUS[d.type] ?? 8)
      .attr("fill", (d) => TYPE_COLORS[d.type] ?? "#6b7280")
      .attr("stroke", (d) => (d.id === selectedId ? "#000" : "#fff"))
      .attr("stroke-width", (d) => (d.id === selectedId ? 3 : 1.5));

    node
      .append("text")
      .text((d) => d.label)
      .attr("font-size", 11)
      .attr("dx", 14)
      .attr("dy", 4)
      .attr("fill", "currentColor")
      .attr("paint-order", "stroke")
      .attr("stroke", "var(--bg-secondary)")
      .attr("stroke-width", 3)
      .attr("stroke-linejoin", "round");

    const sim = d3
      .forceSimulation(nodes)
      .force(
        "link",
        d3
          .forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance(80)
          .strength(0.4)
      )
      .force("charge", d3.forceManyBody<SimNode>().strength(-200))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force(
        "collide",
        d3.forceCollide<SimNode>().radius((d) => (TYPE_RADIUS[d.type] ?? 8) + 4)
      );

    sim.on("tick", () => {
      link
        .attr("x1", (d) => (d.source as SimNode).x ?? 0)
        .attr("y1", (d) => (d.source as SimNode).y ?? 0)
        .attr("x2", (d) => (d.target as SimNode).x ?? 0)
        .attr("y2", (d) => (d.target as SimNode).y ?? 0);
      node.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
    });

    // Zoom / pan
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 5])
      .on("zoom", (event) => {
        root.attr("transform", event.transform.toString());
      });
    svg.call(zoom);

    return () => {
      sim.stop();
    };
  }, [graph, selectedId, selectPage]);

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
          {Object.keys(TYPE_COLORS).map((t) => (
            <span key={t} className="wiki-graph__legend-item">
              <span
                className="wiki-graph__dot"
                style={{ background: TYPE_COLORS[t] }}
              />
              {labelForType(t)}
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
        <svg ref={ref} className="wiki-graph__svg" />
      )}
      <div className="wiki-graph__hint">
        拖动节点 / 滚轮缩放 / 点击页面节点跳转
      </div>
      <style>{graphStyles}</style>
    </div>
  );
}

function labelForType(t: string): string {
  if (t === "page") return "页面";
  if (t.startsWith("entity_")) {
    const kind = t.slice("entity_".length);
    return {
      person: "人物",
      concept: "概念",
      project: "项目",
      contract: "合同",
      law: "法规",
    }[kind] ?? kind;
  }
  return t;
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
  .wiki-graph__svg { flex: 1; background: var(--bg-secondary); }
  .wiki-graph__hint {
    padding: 6px 16px;
    font-size: 11px;
    color: var(--text-tertiary);
    border-top: 1px solid var(--border);
    text-align: center;
  }
`;
