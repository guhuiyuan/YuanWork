/**
 * Text composition for the chat stream.
 *
 * These are pure functions so the ordering rules can be unit tested
 * directly with `node --test`. Getting them wrong is visible to the user
 * as duplicated or reordered text, so they are worth pinning down.
 */

/** The heading the refusal template puts above the positions. */
const POSITIONS_HEADING = "## 最可能相关的位置";

/** The heading the refusal template puts above the advice. */
const ADVICE_HEADING = "## 你可以：";

/**
 * Append a failure notice to text that was already streamed.
 *
 * The notice goes *after* the text, never before it: the user has
 * already read the text, and a notice in front of it reads as if the
 * request failed before anything was produced.
 */
export function appendFailureNotice(
  streamed: string,
  notice: string
): string {
  return streamed ? `${streamed}\n\n${notice}` : notice;
}

/**
 * The final text for a refusal.
 *
 * The backend's refusal message already contains everything that was
 * streamed (the partial answer plus the error notice), so it REPLACES
 * the streamed text rather than being appended to it - appending would
 * duplicate the tail.
 */
export function refusalContent(message: string): string {
  return message;
}

/**
 * Whether a refusal was caused by the model failing rather than by the
 * evidence being insufficient.
 *
 * The two cases need different wording: "证据不足" is a deliberate
 * refusal, "生成出错" is a malfunction. The model can fail in two
 * places - while judging the evidence and while writing the answer -
 * and both come back as "model_unavailable".
 */
export function isGenerationError(reason: string | undefined): boolean {
  if (!reason) return false;
  return reason.startsWith("generation_error") || reason === "model_unavailable";
}

/** The banner shown above a refused or failed answer. */
export function refusalBanner(reason: string | undefined): {
  title: string;
  detail: string;
} {
  if (isGenerationError(reason)) {
    return {
      title: "⚠️ 生成回答时出错",
      detail: "已保留生成到一半的内容，你可以重试一次。",
    };
  }
  if (reason === "citation_invalid") {
    return {
      title: "⚠️ 引用校验没通过，已拒绝直接回答",
      detail: "答案里的引用编号和资料对不上，下面是可能相关的位置。",
    };
  }
  return {
    title: "⚠️ 证据不足，已拒绝直接回答",
    detail: "下方仅供参考可能位置。",
  };
}

/**
 * The refusal text with its list of positions taken out.
 *
 * The text names the positions in words - every reader gets that, and
 * it has to stay that way (ADR-012). The desktop draws the very same
 * positions as buttons that open the file, so there the plain list
 * would say the same thing twice; the buttons take its place.
 *
 * The section is only removed when it is found *and* its end is found
 * as well: a text that does not look the way it is expected to look
 * keeps all of itself, because losing the advice at the end of a
 * refusal is worse than showing a list twice.
 */
export function refusalTextWithoutPositions(text: string): string {
  const start = text.indexOf(POSITIONS_HEADING);
  if (start < 0) return text;
  const end = text.indexOf(ADVICE_HEADING, start);
  if (end < 0) return text;
  return `${text.slice(0, start).trimEnd()}\n\n${text.slice(end)}`.trim();
}

/**
 * Whether the stream ended because the user stopped it.
 *
 * A cancellation is not a failure: the user asked for it, so it gets
 * its own wording instead of the failure notice.
 */
export function isCancellation(detail: string): boolean {
  const d = (detail ?? "").trim();
  if (!d) return false;
  return (
    /cancelled by the user/i.test(d) ||
    /aborted/i.test(d) ||
    /已停止生成/.test(d)
  );
}

/**
 * The notice shown when the request itself failed.
 *
 * The raw error strings are internal and English ("stream ended without
 * a final response", a 150-character reqwest dump). Dumping them into a
 * Chinese UI, styled like an answer, reads as if the assistant had said
 * them. Known failures get a Chinese sentence; anything else is kept but
 * truncated, because an unknown reason is still worth showing.
 */
export function failureNotice(detail: string): string {
  const d = (detail ?? "").trim();
  if (!d) return "（请求失败：未知错误）";
  if (/stream ended without a final response/i.test(d)) {
    return "（请求失败：回答流意外中断）";
  }
  if (/failed to fetch|connection refused|error sending request/i.test(d)) {
    return "（请求失败：无法连接后端服务）";
  }
  if (/^HTTP \d+/i.test(d)) {
    return `（请求失败：${d}）`;
  }
  return `（请求失败：${d.length > 120 ? d.slice(0, 120) + "…" : d}）`;
}
