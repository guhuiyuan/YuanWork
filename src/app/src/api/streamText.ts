/**
 * Text composition for the chat stream.
 *
 * These are pure functions so the ordering rules can be unit tested
 * directly with `node --test`. Getting them wrong is visible to the user
 * as duplicated or reordered text, so they are worth pinning down.
 */

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
 * confidence being too low.
 *
 * The two cases need different wording: "置信度不足" is a deliberate
 * refusal, "生成出错" is a malfunction.
 */
export function isGenerationError(reason: string | undefined): boolean {
  return !!reason && reason.startsWith("generation_error");
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
  return {
    title: "⚠️ 置信度不足，已拒绝直接回答",
    detail: "下方仅供参考可能位置。",
  };
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
