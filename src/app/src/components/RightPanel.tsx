import { useAppStore } from "../store";
import { triggerConsolidation, searchEpisodic } from "../api/backend";
import { useState } from "react";

export function RightPanel() {
  const [tab, setTab] = useState<"trace" | "memory" | "audit">("trace");
  const [episodicResult, setEpisodicResult] = useState<unknown>(null);
  const [consolidationMsg, setConsolidationMsg] = useState<string | null>(null);

  const messages = useAppStore((s) => s.messages);

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
        {tab === "trace" && (
          <div>
            <div className="hint">阶段 2 将可视化 3 步推理轨迹</div>
            {messages.slice(-3).map((m) => (
              <div key={m.id} className="trace-item">
                <div className="trace-item__role">{m.role}</div>
                <div className="trace-item__content">{m.content.slice(0, 100)}</div>
                {m.confidence != null && (
                  <div className="trace-item__meta">置信度 {(m.confidence * 100).toFixed(0)}%</div>
                )}
              </div>
            ))}
          </div>
        )}

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
  .trace-item {
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 8px;
    font-size: 12px;
  }
  .trace-item__role {
    color: var(--text-tertiary);
    text-transform: uppercase;
    font-size: 10px;
    margin-bottom: 2px;
  }
  .trace-item__content {
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .trace-item__meta {
    margin-top: 4px;
    color: var(--text-tertiary);
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
