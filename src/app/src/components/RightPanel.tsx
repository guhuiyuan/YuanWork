import { useAppStore } from "../store";
import { triggerConsolidation, searchEpisodic } from "../api/backend";
import { traceStepLabel, type TraceStep } from "../api/trace.ts";
import { useState } from "react";

export function RightPanel() {
  const [tab, setTab] = useState<"trace" | "memory" | "audit">("trace");
  const [episodicResult, setEpisodicResult] = useState<unknown>(null);
  const [consolidationMsg, setConsolidationMsg] = useState<string | null>(null);

  async function handleConsolidate() {
    setConsolidationMsg("整合中…");
    try {
      const r = await triggerConsolidation();
      setConsolidationMsg(JSON.stringify(r, null, 2));
    } catch (e) {
      setConsolidationMsg(`失败：${(e as Error).message}`);
    }
  }

  async function handleSearchEpisodic() {
    try {
      const r = await searchEpisodic();
      setEpisodicResult(r);
    } catch (e) {
      setEpisodicResult({ error: (e as Error).message });
    }
  }

  return (
    <>
      <div className="panel-header">
        <button
          className={`tab ${tab === "trace" ? "tab--active" : ""}`}
          onClick={() => setTab("trace")}
        >
          推理轨迹
        </button>
        <button
          className={`tab ${tab === "memory" ? "tab--active" : ""}`}
          onClick={() => setTab("memory")}
        >
          记忆
        </button>
        <button
          className={`tab ${tab === "audit" ? "tab--active" : ""}`}
          onClick={() => setTab("audit")}
        >
          审计
        </button>
      </div>

      <div className="panel-body">
        {tab === "trace" && <TracePanel />}

        {tab === "memory" && (
          <div>
            <button className="btn" onClick={handleSearchEpisodic} style={{ width: "100%", marginBottom: 8 }}>
              搜索情景记忆
            </button>
            <button className="btn" onClick={handleConsolidate} style={{ width: "100%", marginBottom: 8 }}>
              触发整合
            </button>
            {consolidationMsg && <pre className="result">{consolidationMsg}</pre>}
            {episodicResult != null && <pre className="result">{JSON.stringify(episodicResult, null, 2)}</pre>}
          </div>
        )}

        {tab === "audit" && (
          <div className="hint">
            <p>审计日志（阶段 3 启用）</p>
            <p style={{ marginTop: 8, fontSize: 12 }}>
              记录每次 LLM 调用的时间、token 数、模型、查询摘要（不存全文）。
            </p>
          </div>
        )}
      </div>

      <style>{rightStyles}</style>
    </>
  );
}

/**
 * The steps of the run that is going on - or of the last one.
 *
 * The backend sends one event per step; this shows what each step
 * found or decided (ADR-053). The steps are the point - the reader
 * wants to see *why* the answer came out the way it did.
 */
function TracePanel() {
  const trace = useAppStore((s) => s.trace);

  return (
    <div>
      <div className="hint hint--left">
        上一次回答实际走过的步骤 —— 能不能作答由「证据闸」决定，置信度只是参考。
      </div>
      {trace.length === 0 ? (
        <div className="hint">
          还没有轨迹 —— 提一个问题，这里会显示它实际走过的步骤。
        </div>
      ) : (
        trace.map((step) => (
          <div
            key={step.step}
            className={`trace-step trace-step--${step.outcome ?? "ok"}`}
          >
            <div className="trace-step__head">
              <span className="trace-step__name">{traceStepLabel(step.step)}</span>
              <span className="trace-step__status">{traceStatusLabel(step)}</span>
            </div>
            <div className="trace-step__detail">{step.detail}</div>
          </div>
        ))
      )}
    </div>
  );
}

/** What happened to a step, in the reader's words. */
function traceStatusLabel(step: TraceStep): string {
  if (step.status === "started") return "进行中";
  if (step.status === "cancelled") return "已停止";
  if (step.durationMs === undefined) return "已完成";
  return step.durationMs < 1000
    ? `${Math.round(step.durationMs)} 毫秒`
    : `${(step.durationMs / 1000).toFixed(1)} 秒`;
}

const rightStyles = `
  .tab {
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }
  .tab--active {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }
  .hint {
    color: var(--text-tertiary);
    font-size: 12px;
    text-align: center;
    padding: 24px 8px;
  }
  .hint--left {
    text-align: left;
    padding: 8px 0 10px;
    line-height: 1.5;
  }
  .trace-step {
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-left: 2px solid var(--border);
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 6px;
    font-size: 12px;
  }
  .trace-step--ok {
    border-left-color: var(--text-tertiary);
  }
  .trace-step--refused {
    border-left-color: #dc2626;
  }
  .trace-step--conflict {
    border-left-color: #d97706;
  }
  .trace-step--skipped {
    border-left-color: var(--border);
    opacity: 0.75;
  }
  .trace-step__head {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 2px;
  }
  .trace-step__name {
    font-weight: 600;
  }
  .trace-step__status {
    color: var(--text-tertiary);
    font-size: 11px;
    white-space: nowrap;
  }
  .trace-step__detail {
    color: var(--text-secondary);
    line-height: 1.5;
    word-break: break-word;
  }
  .result {
    font-family: var(--font-mono);
    font-size: 11px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
`;
