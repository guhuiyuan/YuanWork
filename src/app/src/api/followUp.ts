/**
 * 追问的上下文 —— **只有位置与读者自己上一句问话**（ADR-057）。
 *
 * 上一轮模型生成的回答**不进这里**：它没有出处，带进提示词等于开了一条
 * 「不用证据也能说」的通道。带进来的两样东西都是可核验的——
 * 位置指向原文，读者上一句问话是读者的意图（「它」指什么），不是结论。
 *
 * 这一层是会话内的：随请求带过去，不落盘。
 */

import type { Citation, ChatMessage } from "../store";

/** A place in the original - the previous answer pointed here. */
export interface FollowUpPosition {
  file_name: string;
  page?: number;
  slide_number?: number;
  section?: string | null;
  sheet?: string | null;
  row?: number | null;
  location?: string | null;
}

/** What the reader asked last time, and where the last answer pointed. */
export interface FollowUpContext {
  /** The reader's own question - their intent, not a conclusion. */
  question: string;
  /** Where the previous answer pointed - at most ten. */
  positions: FollowUpPosition[];
}

/** The positions are capped - the prompt must not grow with the chat. */
export const MAX_FOLLOW_UP_POSITIONS = 10;
/** The reader's question is capped for the same reason. */
export const MAX_FOLLOW_UP_QUESTION = 500;

function positionOf(citation: Citation): FollowUpPosition | null {
  const fileName = (citation.file_name || "").trim();
  if (!fileName) return null;
  return {
    file_name: fileName,
    page: citation.page,
    slide_number: citation.slide_number,
    section: citation.section ?? null,
    sheet: citation.sheet ?? null,
    row: citation.row ?? null,
    location: citation.location ?? null,
  };
}

/**
 * The context of the previous turn - or null when there is none.
 *
 * It is only worth sending when the previous turn actually answered
 * something with positions: without them there is nothing to carry
 * over, and a bare question would only add noise to the prompt.
 */
export function buildFollowUp(messages: ChatMessage[]): FollowUpContext | null {
  const answered = [...messages]
    .reverse()
    .find((m) => m.role === "assistant" && (m.citations?.length ?? 0) > 0);
  if (!answered) return null;

  const asked = [...messages]
    .reverse()
    .find((m) => m.role === "user" && m.content.trim().length > 0);
  if (!asked) return null;

  const positions: FollowUpPosition[] = [];
  for (const citation of answered.citations ?? []) {
    const position = positionOf(citation);
    if (position) positions.push(position);
    if (positions.length >= MAX_FOLLOW_UP_POSITIONS) break;
  }
  if (positions.length === 0) return null;

  return {
    question: asked.content.trim().slice(0, MAX_FOLLOW_UP_QUESTION),
    positions,
  };
}
