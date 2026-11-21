import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore, ChatMessage, Citation, RefusalPosition, TermContext } from "../store";
import {
  cancelChat,
  chatStream,
  ChatResponse,
  ConflictInfo,
  getTermContext,
  openCitation,
} from "../api/backend";
import { citationLabel, openedNotice } from "../api/citations";
import { termContextSummary } from "../api/terms";
import { AnswerMarkdown } from "./AnswerMarkdown";
import { appendFailureNotice, failureNotice, isCancellation, refusalBanner, refusalContent, refusalTextWithoutPositions } from "../api/streamText";

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
  const flushTimerRef = useRef<number | null>(null);
  const gotTokensRef = useRef(false);
  const answerTextRef = useRef("");
  const errorNoticedRef = useRef(false);
  // The session of the run that is going on - the stop button needs it.
  const sessionIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const flush = useCallback(() => {
    // Cancel whichever handle is pending - the other one may already
    // have fired.
    if (flushHandleRef.current !== null) {
      if (typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(flushHandleRef.current);
      } else {
        clearTimeout(flushHandleRef.current);
      }
      flushHandleRef.current = null;
    }
    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const pending = pendingRef.current;
    if (!pending) return;
    pendingRef.current = "";
    const id = streamIdRef.current;
    if (id) useAppStore.getState().appendToMessage(id, pending);
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushHandleRef.current !== null || flushTimerRef.current !== null) return;
    if (typeof requestAnimationFrame === "function") {
      flushHandleRef.current = requestAnimationFrame(() => {
        flushHandleRef.current = null;
        flush();
      });
      // Safety net: requestAnimationFrame does NOT fire while the tab is
      // hidden, so without this the whole answer would sit in the buffer
      // until the stream ends and the UI would look frozen.
      flushTimerRef.current = window.setTimeout(() => {
        flushTimerRef.current = null;
        flush();
      }, 100) as unknown as number;
    } else {
      flushTimerRef.current = window.setTimeout(() => {
        flushTimerRef.current = null;
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
      if (flushTimerRef.current !== null) {
        clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
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
    // The trace belongs to one run: a new question starts a new one.
    useAppStore.getState().clearTrace();

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

    // The session identifies the run - the stop button needs it to
    // reach the generation that is going on.
    const sessionId = crypto.randomUUID();
    sessionIdRef.current = sessionId;

    try {
      const resp: ChatResponse = await chatStream({ query: q, session_id: sessionId }, (ev) => {
        switch (ev.type) {
          case "trace": {
            // The steps of the run. The panel on the right shows them
            // as they happen (ADR-053).
            useAppStore.getState().applyTrace(ev.data);
            break;
          }
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
          case "done": {
            // The terms of the answer travel with the final response.
            // They are read here as well, because the desktop bridge
            // hands the answer back without them.
            const response = ev.data.response as
              | { terms?: string[] }
              | undefined;
            const terms = response?.terms;
            if (terms && terms.length > 0) {
              useAppStore.getState().updateMessage(assistantId, { terms });
            }
            break;
          }
          case "refusal": {
            const message =
              typeof ev.data.message === "string" ? ev.data.message : "";
            const reason =
              typeof ev.data.reason === "string" ? ev.data.reason : undefined;
            // The refusal message already contains everything that was
            // streamed, so the pending buffer is flushed first and then
            // REPLACED. Appending would duplicate the tail.
            const positions = Array.isArray(ev.data.positions)
              ? (ev.data.positions as RefusalPosition[])
              : [];
            flush();
            useAppStore.getState().updateMessage(assistantId, {
              refused: true,
              refusalReason: reason,
              content: refusalContent(message),
              refusalPositions: positions,
            });
            // The reason belongs on the trace too - it says why the
            // run ended without an answer.
            useAppStore.getState().applyRefusalReason(reason);
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
            // Flush first, otherwise the notice would be inserted in
            // front of the text that was already streamed.
            flush();
            const current = useAppStore
              .getState()
              .messages.find((m) => m.id === assistantId);
            const notice = failureNotice(detail);
            errorNoticedRef.current = true;
            useAppStore.getState().updateMessage(assistantId, {
              content: appendFailureNotice(current?.content ?? "", notice),
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
        terms: resp.terms,
      };
      // The reason tells the banner apart: a deliberate refusal
      // ("low_confidence") and a model failure ("generation_error: ...")
      // look identical in the response body.
      if (resp.refusal_reason) {
        patch.refusalReason = resp.refusal_reason;
      }
      if (resp.refusal_positions?.length) {
        patch.refusalPositions = resp.refusal_positions;
      }
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
      // Tauri rejects an `Err(String)` command with the bare string, not
      // an Error - so `e.message` would be undefined and the user would
      // see "（请求失败：undefined）". Handle both shapes.
      const detail =
        e instanceof Error
          ? e.message
          : typeof e === "string"
            ? e
            : (() => {
                try {
                  return JSON.stringify(e);
                } catch {
                  return String(e);
                }
              })();
      const current = useAppStore
        .getState()
        .messages.find((m) => m.id === assistantId);
      // A cancellation is not a failure - the user asked for it.
      if (isCancellation(detail)) {
        const base = current?.content || answerTextRef.current;
        useAppStore.getState().updateMessage(assistantId, {
          content: base ? `${base}\n\n（已停止生成）` : "（已停止生成）",
        });
      } else if (!errorNoticedRef.current) {
        // Keep whatever was already streamed - losing it would be worse
        // than showing the error next to it. If an `error` event already
        // reported the failure, do not repeat the notice.
        // The backend's "answer" event carries the complete text. If the
        // stream was cut before "done", that is the only place the full
        // answer exists - fall back to it instead of showing nothing.
        const base = current?.content || answerTextRef.current;
        const notice = failureNotice(detail);
        useAppStore.getState().updateMessage(assistantId, {
          content: appendFailureNotice(base, notice),
        });
      }
    } finally {
      setStreaming(false);
      streamIdRef.current = null;
      sessionIdRef.current = null;
    }
  }

  // Stop the generation that is going on.
  const handleStop = useCallback(async () => {
    const sessionId = sessionIdRef.current;
    if (!sessionId) return;
    try {
      await cancelChat(sessionId);
    } catch (e) {
      // The stream ends on its own anyway - the user is not left with a
      // dialog about it.
      console.warn("Could not stop the generation:", e);
    }
  }, []);

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
        {isStreaming && (
          <button className="btn btn--stop" onClick={() => void handleStop()}>
            停止
          </button>
        )}
      </div>

      <style>{chatStyles}</style>
    </div>
  );
}

function Message({ msg }: { msg: ChatMessage }) {
  const showToast = useAppStore((s) => s.showToast);
  const [openError, setOpenError] = useState<string | null>(null);
  // The term whose context is open, and the context itself.
  const [activeTerm, setActiveTerm] = useState<string | null>(null);
  const [termContext, setTermContext] = useState<TermContext | null>(null);
  const [termLoading, setTermLoading] = useState(false);
  const [termError, setTermError] = useState<string | null>(null);
  // Counts the lookups, so a slow answer for a term the reader has
  // already left does not overwrite the one that is open.
  const termRequestRef = useRef(0);

  // Clicking a citation opens the file it points at, at the position it
  // points to (ADR-017). A file that cannot be opened says so instead
  // of doing nothing - the reader has to know the click did not work.
  //
  // The file opens in the default program, and most programs cannot be
  // told "show page 12" - so the app says where to look. Without that
  // the reader is left with a document and no idea where in it the
  // words are.
  // A refusal names the places where the answer probably is. The
  // refusal text tells the reader to click them - so they have to be
  // clickable, with the path the backend sent (TASK-085).
  const openPositionAt = useCallback(async (position: RefusalPosition) => {
    setOpenError(null);
    if (!position.file_path) {
      setOpenError("这个位置没有文件路径。");
      return;
    }
    try {
      const opened = await openCitation({
        index: 0,
        file_name: position.file_name,
        file_path: position.file_path,
        text: "",
        location: position.location,
      } as Citation);
      showToast(
        openedNotice(
          { file_name: position.file_name, location: position.location },
          opened
        ),
        "info"
      );
    } catch (e) {
      setOpenError(e instanceof Error ? e.message : String(e));
    }
  }, [showToast]);

  const openCitationAt = useCallback(async (citation: Citation) => {
    setOpenError(null);
    try {
      const opened = await openCitation(citation);
      showToast(openedNotice(citation, opened), "info");
    } catch (e) {
      setOpenError(e instanceof Error ? e.message : String(e));
    }
  }, [showToast]);

  /**
   * Open the text a term stands for.
   *
   * The passages come from the knowledge base with the place they were
   * read from - nothing is generated here (ADR-041). A term the
   * knowledge base does not hold says so.
   */
  const openTerm = useCallback(async (term: string) => {
    const request = ++termRequestRef.current;
    setActiveTerm(term);
    setTermLoading(true);
    setTermError(null);
    setTermContext(null);
    try {
      const context = await getTermContext(term);
      if (request !== termRequestRef.current) return;
      setTermContext(context);
    } catch (e) {
      if (request !== termRequestRef.current) return;
      setTermError(e instanceof Error ? e.message : String(e));
    } finally {
      if (request === termRequestRef.current) setTermLoading(false);
    }
  }, []);

  const closeTerm = useCallback(() => {
    termRequestRef.current += 1;
    setActiveTerm(null);
    setTermContext(null);
    setTermError(null);
    setTermLoading(false);
  }, []);

  // The answer, rendered as markdown - the model writes it that way,
  // and the marks themselves should not be what the reader sees
  // (TASK-086). The text itself is not changed: what is rendered is
  // what was written.
  //
  // A refusal is the one case where the text loses a part: its list of
  // positions is drawn as buttons above the message, so the plain list
  // would only say the same thing twice.
  const hasPositions = !!msg.refusalPositions && msg.refusalPositions.length > 0;
  const shownContent = useMemo(
    () => (hasPositions ? refusalTextWithoutPositions(msg.content) : msg.content),
    [hasPositions, msg.content]
  );

  if (msg.role === "user") {
    return (
      <div className="msg msg--user">
        <div className="msg__bubble">{msg.content}</div>
      </div>
    );
  }

  // Assistant message
  const banner = msg.refused ? refusalBanner(msg.refusalReason) : null;
  return (
    <div className="msg msg--assistant">
      {banner && (
        <div className="msg__refusal">
          <strong>{banner.title}</strong>
          <div>{banner.detail}</div>
        </div>
      )}

      {msg.conflict && (
        <div className="msg__conflict">
          <strong>⚠️ 冲突提示</strong>
          <div>{msg.conflict}</div>
        </div>
      )}

      {msg.refusalPositions && msg.refusalPositions.length > 0 && (
        <div className="msg__citations msg__citations--positions">
          <div className="msg__citations-title">最可能相关的位置（点开看原文）</div>
          {msg.refusalPositions.map((p, index) => (
            <button
              key={`${p.file_path}-${p.location}-${index}`}
              className="citation"
              onClick={() => void openPositionAt(p)}
              title="打开原文"
            >
              <span className="citation__index">{index + 1}</span>
              <span className="citation__body">
                <span className="citation__loc">
                  📄 《{p.file_name}》
                  {p.location ? ` · ${p.location}` : ""}
                </span>
              </span>
            </button>
          ))}
          {openError && (
            <div className="citation__error">打不开这个文件：{openError}</div>
          )}
        </div>
      )}

      <div className="msg__content">
        {msg.content ? (
          <AnswerMarkdown
            content={shownContent}
            terms={msg.terms ?? []}
            onTerm={(term) => void openTerm(term)}
          />
        ) : (
          "（生成中…）"
        )}
      </div>

      {msg.terms && msg.terms.length > 0 && (
        <div className="msg__terms">
          <span className="msg__terms-title">术语</span>
          {msg.terms.map((term, index) => (
            <button
              key={`${term}-${index}`}
              type="button"
              className="msg__term-chip"
              onClick={() => void openTerm(term)}
            >
              {term}
            </button>
          ))}
        </div>
      )}

      {activeTerm && (
        <div className="msg__term-panel">
          <div className="msg__term-panel-head">
            <span>「{activeTerm}」的原文上下文</span>
            <button
              type="button"
              className="msg__term-panel-close"
              onClick={closeTerm}
            >
              关闭
            </button>
          </div>
          {termLoading && <div className="msg__term-status">查找中…</div>}
          {termError && (
            <div className="msg__term-status msg__term-status--error">
              查不到：{termError}
            </div>
          )}
          {termContext && (
            <>
              <div className="msg__term-summary">
                {termContextSummary(termContext)}
              </div>
              {termContext.contexts.map((item, index) => (
                <div
                  key={item.chunk_id || index}
                  className="msg__term-passage"
                >
                  <div className="msg__term-passage-text">{item.text}</div>
                  <div className="msg__term-passage-loc">
                    {item.file_name}
                    {item.location ? ` · ${item.location}` : ""}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      )}

      {msg.citations && msg.citations.length > 0 && (
        <div className="msg__citations">
          <div className="msg__citations-title">引用来源</div>
          {msg.citations.map((c) => (
            <button
              key={c.index}
              className="citation"
              onClick={() => void openCitationAt(c)}
              title="打开原文"
            >
              <span className="citation__index">[{c.index}]</span>
              <span className="citation__body">
                <span className="citation__loc">📄 {citationLabel(c)}</span>
                <span className="citation__text">「{c.text}」</span>
              </span>
            </button>
          ))}
          {openError && (
            <div className="citation__error">打不开这个文件：{openError}</div>
          )}
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
  .chat__input .btn--stop {
    align-self: flex-end;
    white-space: nowrap;
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
    word-wrap: break-word;
  }
  /* The answer is markdown: it brings its own headings, lists and
     emphasis, so the marks must not show up as text (TASK-086). */
  .msg__content > *:first-child { margin-top: 0; }
  .msg__content > *:last-child { margin-bottom: 0; }
  .msg__content h1, .msg__content h2, .msg__content h3, .msg__content h4 {
    margin: 14px 0 6px;
    line-height: 1.35;
    color: var(--text-primary);
  }
  .msg__content h1 { font-size: 18px; }
  .msg__content h2 { font-size: 16px; }
  .msg__content h3 { font-size: 15px; }
  .msg__content h4 { font-size: 14px; }
  .msg__content p { margin: 0 0 8px; }
  .msg__content ul, .msg__content ol { margin: 0 0 8px; padding-left: 22px; }
  .msg__content li { margin: 3px 0; }
  .msg__content li > p { margin: 0; }
  .msg__content strong { font-weight: 600; color: var(--text-primary); }
  .msg__content code {
    font-family: var(--font-mono);
    font-size: 12px;
    background: var(--bg-tertiary);
    padding: 1px 5px;
    border-radius: 4px;
  }
  .msg__content pre {
    background: var(--bg-tertiary);
    padding: 10px 12px;
    border-radius: 6px;
    overflow-x: auto;
    margin: 0 0 8px;
  }
  .msg__content pre code { background: none; padding: 0; }
  .msg__content blockquote {
    margin: 0 0 8px;
    padding: 2px 0 2px 10px;
    border-left: 3px solid var(--border);
    color: var(--text-secondary);
  }
  .msg__content table {
    border-collapse: collapse;
    margin: 0 0 8px;
    font-size: 13px;
  }
  .msg__content th, .msg__content td {
    border: 1px solid var(--border);
    padding: 5px 8px;
    text-align: left;
  }
  .msg__content th { background: var(--bg-tertiary); }
  .msg__content hr { border: none; border-top: 1px solid var(--border); margin: 12px 0; }
  .md__link { color: var(--accent); text-decoration: underline; }
  .md__link-url { color: var(--text-tertiary); font-size: 11px; margin-left: 4px; }
  .md__image { color: var(--text-secondary); font-style: italic; }
  .msg__term {
    display: inline;
    background: none;
    border: none;
    border-bottom: 1px dotted var(--accent);
    padding: 0;
    font: inherit;
    color: var(--accent);
    cursor: pointer;
  }
  .msg__term:hover { border-bottom-style: solid; }
  .msg__terms {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 6px;
    max-width: 85%;
  }
  .msg__terms-title {
    font-size: 11px;
    color: var(--text-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .msg__term-chip {
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1px 10px;
    font-size: 12px;
    color: var(--text-secondary);
    font-family: inherit;
    cursor: pointer;
  }
  .msg__term-chip:hover { border-color: var(--accent); color: var(--accent); }
  .msg__term-panel {
    margin-top: 6px;
    max-width: 85%;
    background: var(--bg-secondary);
    border-left: 3px solid var(--accent);
    border-radius: 4px;
    padding: 8px 12px;
    font-size: 13px;
  }
  .msg__term-panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    color: var(--text-secondary);
    font-size: 12px;
    margin-bottom: 6px;
  }
  .msg__term-panel-close {
    background: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 11px;
    padding: 0 6px;
  }
  .msg__term-panel-close:hover { border-color: var(--accent); }
  .msg__term-status {
    color: var(--text-secondary);
    font-size: 12px;
  }
  .msg__term-status--error { color: var(--conflict-border, #991b1b); }
  .msg__term-summary {
    color: var(--text-secondary);
    font-size: 12px;
    margin-bottom: 6px;
  }
  .msg__term-passage {
    border-top: 1px solid var(--border);
    padding: 6px 0;
  }
  .msg__term-passage-text {
    color: var(--text-primary);
    line-height: 1.6;
    white-space: pre-wrap;
  }
  .msg__term-passage-loc {
    color: var(--text-tertiary);
    font-size: 11px;
    margin-top: 2px;
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
    border: none;
    border-left: 3px solid var(--citation-border);
    border-radius: 4px;
    padding: 8px 10px;
    margin-bottom: 4px;
    font-size: 13px;
    font-family: inherit;
    color: inherit;
    text-align: left;
    width: 100%;
    cursor: pointer;
  }
  .citation:hover {
    background: var(--citation-hover-bg, var(--citation-bg));
    border-left-color: var(--accent);
  }
  .citation__index {
    color: var(--accent);
    font-weight: 600;
    flex-shrink: 0;
  }
  .citation__body { flex: 1; display: block; }
  .citation__loc {
    display: block;
    font-size: 12px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .citation__text {
    display: block;
    color: var(--text-primary);
    font-style: italic;
  }
  .citation__error {
    font-size: 11px;
    color: var(--conflict-border, #991b1b);
    margin-top: 4px;
  }
  .msg__confidence {
    font-size: 11px;
    color: var(--text-tertiary);
    margin-top: 4px;
  }
`;
