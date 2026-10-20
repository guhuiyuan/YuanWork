import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore, ChatMessage } from "../store";
import { chatStream, ChatResponse, ConflictInfo } from "../api/backend";

/** Format the backend's conflict records into one warning string. */
function formatConflicts(conflicts: unknown): string | undefined {
  if (!Array.isArray(conflicts) || conflicts.length === 0) return undefined;
  return conflicts
    .map((c) => {
      const info = c as ConflictInfo;
      if (info.message) return info.message;
      const topic = info.topic ?? "冲突";
      return `${topic}：${info.llm_says ?? ""} / ${info.kb_says ?? ""}`;
    })
    .join("\n");
}

export function ChatWindow() {
  const messages = useAppStore((s) => s.messages);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const setStreaming = useAppStore((s) => s.setStreaming);
  const addMessage = useAppStore((s) => s.addMessage);
  const clearMessages = useAppStore((s) => s.clearMessages);

  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  // The message the stream is currently writing into, plus the token
  // buffer that is flushed to the store on the next animation frame.
  const streamIdRef = useRef<string | null>(null);
  const pendingRef = useRef("");
  const flushHandleRef = useRef<number | null>(null);
  const gotTokensRef = useRef(false);
  const answerTextRef = useRef("");
  const errorNoticedRef = useRef(false);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const flush = useCallback(() => {
    flushHandleRef.current = null;
    const pending = pendingRef.current;
    if (!pending) return;
    pendingRef.current = "";
    const id = streamIdRef.current;
    if (id) useAppStore.getState().appendToMessage(id, pending);
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushHandleRef.current !== null) return;
    if (typeof requestAnimationFrame === "function") {
      flushHandleRef.current = requestAnimationFrame(() => {
        flushHandleRef.current = null;
        flush();
      });
    } else {
      flushHandleRef.current = window.setTimeout(() => {
        flushHandleRef.current = null;
        flush();
      }, 50) as unknown as number;
    }
  }, [flush]);

  // Cancel a pending flush if the component goes away mid-stream.
  useEffect(() => {
    return () => {
      if (flushHandleRef.current !== null) {
        if (typeof cancelAnimationFrame === "function") {
          cancelAnimationFrame(flushHandleRef.current);
        } else {
          clearTimeout(flushHandleRef.current);
        }
        flushHandleRef.current = null;
      }
    };
  }, []);

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

    // Placeholder assistant message - the stream writes into this one.
    const assistantId = crypto.randomUUID();
    addMessage({
      id: assistantId,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
    });
    streamIdRef.current = assistantId;
    pendingRef.current = "";
    gotTokensRef.current = false;
    answerTextRef.current = "";
    errorNoticedRef.current = false;

    try {
      const resp: ChatResponse = await chatStream({ query: q }, (ev) => {
        switch (ev.type) {
          case "token": {
            const text = typeof ev.data.text === "string" ? ev.data.text : "";
            if (text) {
              gotTokensRef.current = true;
              pendingRef.current += text;
              scheduleFlush();
            }
            break;
          }
          case "answer": {
            // The full text. Only used as a fallback when no token
            // events arrived - appending it as well would duplicate it.
            if (typeof ev.data.text === "string") {
              answerTextRef.current = ev.data.text;
            }
            break;
          }
          case "citations": {
            const citations = ev.data.citations as ChatMessage["citations"];
            if (citations) {
              useAppStore.getState().updateMessage(assistantId, { citations });
            }
            break;
          }
          case "refusal": {
            const message =
              typeof ev.data.message === "string" ? ev.data.message : "";
            useAppStore.getState().updateMessage(assistantId, {
              refused: true,
              content: message,
            });
            break;
          }
          case "conflict": {
            const conflict = formatConflicts(ev.data.conflicts);
            useAppStore.getState().updateMessage(assistantId, { conflict });
            break;
          }
          case "error": {
            const detail =
              typeof ev.data.detail === "string" ? ev.data.detail : "未知错误";
            // Keep whatever was already streamed - losing it would be
            // worse than showing the error next to it.
            const current = useAppStore
              .getState()
              .messages.find((m) => m.id === assistantId);
            const notice = `（请求失败：${detail}）`;
            errorNoticedRef.current = true;
            useAppStore.getState().updateMessage(assistantId, {
              content: current?.content
                ? `${current.content}\n\n${notice}`
                : notice,
            });
            break;
          }
          default:
            break;
        }
      });

      // Make sure nothing is left in the buffer.
      flush();

      const patch: Partial<ChatMessage> = {
        citations: resp.citations,
        confidence: resp.confidence,
        refused: resp.refused,
      };
      const conflict = formatConflicts(resp.conflicts);
      if (conflict) patch.conflict = conflict;
      // If no token event ever arrived, fall back to the full answer.
      const fallback = answerTextRef.current || resp.answer;
      if (!gotTokensRef.current && fallback) {
        patch.content = fallback;
      }
      useAppStore.getState().updateMessage(assistantId, patch);
    } catch (e) {
      flush();
      const detail = (e as Error).message;
      const current = useAppStore
        .getState()
        .messages.find((m) => m.id === assistantId);
      // Keep whatever was already streamed - losing it would be worse
      // than showing the error next to it. If an `error` event already
      // reported the failure, do not repeat the notice.
      if (!errorNoticedRef.current) {
        const content = current?.content
          ? `${current.content}\n\n（请求失败：${detail}）`
          : `（请求失败：${detail}）`;
        useAppStore.getState().updateMessage(assistantId, { content });
      }
    } finally {
      setStreaming(false);
      streamIdRef.current = null;
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
