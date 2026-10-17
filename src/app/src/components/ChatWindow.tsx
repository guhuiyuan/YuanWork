import { useEffect, useRef, useState } from "react";
import { useAppStore, ChatMessage } from "../store";
import { chat } from "../api/backend";

export function ChatWindow() {
  const messages = useAppStore((s) => s.messages);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const setStreaming = useAppStore((s) => s.setStreaming);
  const addMessage = useAppStore((s) => s.addMessage);
  const clearMessages = useAppStore((s) => s.clearMessages);

  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  async function handleSend() {
    const q = input.trim();
    if (!q || isStreaming) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: q,
      timestamp: Date.now(),
    };
    addMessage(userMsg);
    setInput("");
    setStreaming(true);

    // Placeholder assistant message
    addMessage({
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      timestamp: Date.now(),
    });

    try {
      const resp = await chat({ query: q });
      // Replace the placeholder with the final response
      useAppStore.setState((s) => {
        const msgs = [...s.messages];
        const idx = msgs.length - 1;
        if (idx >= 0) {
          msgs[idx] = {
            ...msgs[idx],
            content: resp.answer,
            citations: resp.citations,
            confidence: resp.confidence,
            refused: resp.refused,
            conflict: resp.conflict ?? undefined,
          };
        }
        return { messages: msgs };
      });
    } catch (e) {
      useAppStore.setState((s) => {
        const msgs = [...s.messages];
        const idx = msgs.length - 1;
        if (idx >= 0) {
          msgs[idx] = {
            ...msgs[idx],
            content: `（请求失败：${(e as Error).message}）`,
          };
        }
        return { messages: msgs };
      });
    } finally {
      setStreaming(false);
    }
  }

  return (
    <div className="chat">
      <div className="chat__header">
        <span>对话</span>
        {messages.length > 0 && (
          <button className="btn" onClick={clearMessages}>
            清空
          </button>
        )}
      </div>

      <div className="chat__messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="chat__empty">
            <h2>👋 你好，我是 YuanWork</h2>
            <p>本地知识库助手 · 严格引用 · 隐私优先</p>
            <div className="chat__suggestions">
              <SuggestionChip text="这份合同里违约金怎么约定？" onSelect={setInput} />
              <SuggestionChip text="上次我们讨论过的方案三是什么？" onSelect={setInput} />
              <SuggestionChip text="总结一下这个产品 PPT 的核心功能" onSelect={setInput} />
            </div>
          </div>
        )}

        {messages.map((m) => (
          <Message key={m.id} msg={m} />
        ))}

        {isStreaming && messages[messages.length - 1]?.content === "" && (
          <div className="chat__thinking">思考中…</div>
        )}
      </div>

      <div className="chat__input">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          rows={3}
          disabled={isStreaming}
        />
        <button
          className="btn btn--primary"
          onClick={handleSend}
          disabled={isStreaming || !input.trim()}
        >
          发送
        </button>
      </div>

      <style>{chatStyles}</style>
    </div>
  );
}

function Message({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") {
    return (
      <div className="msg msg--user">
        <div className="msg__bubble">{msg.content}</div>
      </div>
    );
  }

  // Assistant message
  return (
    <div className="msg msg--assistant">
      {msg.refused && (
        <div className="msg__refusal">
          <strong>⚠️ 置信度不足，已拒绝直接回答</strong>
          <div>下方仅供参考可能位置。</div>
        </div>
      )}

      {msg.conflict && (
        <div className="msg__conflict">
          <strong>⚠️ 冲突提示</strong>
          <div>{msg.conflict}</div>
        </div>
      )}

      <div className="msg__content">{msg.content || "（生成中…）"}</div>

      {msg.citations && msg.citations.length > 0 && (
        <div className="msg__citations">
          <div className="msg__citations-title">引用来源</div>
          {msg.citations.map((c) => (
            <div key={c.index} className="citation">
              <span className="citation__index">[{c.index}]</span>
              <div className="citation__body">
                <div className="citation__loc">
                  📄 {c.file_name}
                  {c.page != null && ` · 第 ${c.page} 页`}
                  {c.slide_number != null && ` · Slide ${c.slide_number}`}
                </div>
                <div className="citation__text">「{c.text}」</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {msg.confidence != null && (
        <div className="msg__confidence">
          置信度：{(msg.confidence * 100).toFixed(0)}%
        </div>
      )}
    </div>
  );
}

function SuggestionChip({ text, onSelect }: { text: string; onSelect: (s: string) => void }) {
  return (
    <button className="chip" onClick={() => onSelect(text)}>
      {text}
    </button>
  );
}

const chatStyles = `
  .chat {
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  .chat__header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
  }
  .chat__messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
  }
  .chat__empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-secondary);
    gap: 12px;
  }
  .chat__empty h2 { color: var(--text-primary); }
  .chat__suggestions {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 16px;
  }
  .chip {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    padding: 8px 12px;
    border-radius: 16px;
    color: var(--text-secondary);
    font-size: 13px;
    transition: all 0.15s;
  }
  .chip:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }
  .chat__input {
    display: flex;
    gap: 8px;
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    background: var(--bg-primary);
    flex-shrink: 0;
  }
  .chat__input textarea {
    flex: 1;
    resize: none;
    min-height: 60px;
  }
  .chat__thinking {
    color: var(--text-tertiary);
    font-size: 12px;
    padding: 8px 0;
    font-style: italic;
  }

  .msg {
    display: flex;
    margin-bottom: 16px;
  }
  .msg--user { justify-content: flex-end; }
  .msg--assistant { justify-content: flex-start; flex-direction: column; }
  .msg__bubble {
    background: var(--accent);
    color: white;
    padding: 8px 12px;
    border-radius: 12px;
    max-width: 70%;
    white-space: pre-wrap;
  }
  .msg__content {
    background: var(--bg-secondary);
    padding: 10px 14px;
    border-radius: 8px;
    max-width: 85%;
    white-space: pre-wrap;
    word-wrap: break-word;
  }
  .msg__refusal {
    background: var(--refusal-bg);
    border-left: 3px solid var(--refusal-border);
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 8px;
    font-size: 13px;
    color: #92400e;
  }
  .msg__conflict {
    background: var(--conflict-bg);
    border-left: 3px solid var(--conflict-border);
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 8px;
    font-size: 13px;
    color: #991b1b;
  }
  .msg__citations {
    margin-top: 8px;
    max-width: 85%;
  }
  .msg__citations-title {
    font-size: 11px;
    color: var(--text-tertiary);
    margin-bottom: 4px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .citation {
    display: flex;
    gap: 8px;
    background: var(--citation-bg);
    border-left: 3px solid var(--citation-border);
    padding: 8px 10px;
    border-radius: 4px;
    margin-bottom: 4px;
    font-size: 13px;
  }
  .citation__index {
    color: var(--accent);
    font-weight: 600;
    flex-shrink: 0;
  }
  .citation__body { flex: 1; }
  .citation__loc {
    font-size: 12px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .citation__text {
    color: var(--text-primary);
    font-style: italic;
  }
  .msg__confidence {
    font-size: 11px;
    color: var(--text-tertiary);
    margin-top: 4px;
  }
`;
