/**
 * The terms in an answer, and the text they stand for.
 *
 * A term the reader may not know opens the passages it appears in -
 * the original text with the place it came from, never a generated
 * explanation (ADR-041). Everything in this file is pure: it takes
 * text and returns text, so it can be tested without a browser.
 */

import type { TermContext, TermContextItem } from "../store.ts";

/** A piece of a text: plain, or a term that was found in it. */
export interface TermSegment {
  text: string;
  /** The term this piece is - missing when the piece is plain text. */
  term?: string;
}

/**
 * Split a text into the plain parts and the terms inside it.
 *
 * The longest term wins: a term that sits inside a longer term is not
 * marked a second time. A term that is not in the text is not marked
 * at all. The text comes back unchanged - the pieces joined together
 * are exactly the text that went in.
 */
export function markTerms(text: string, terms: string[]): TermSegment[] {
  if (!text) return [];
  const wanted = wantedTerms(terms);
  if (wanted.length === 0) return [{ text }];

  const segments: TermSegment[] = [];
  let plainStart = 0;
  let index = 0;
  while (index < text.length) {
    const hit = longestAt(text, index, wanted);
    if (hit) {
      if (index > plainStart) {
        segments.push({ text: text.slice(plainStart, index) });
      }
      segments.push({ text: text.slice(index, index + hit.length), term: hit.term });
      index += hit.length;
      plainStart = index;
      continue;
    }
    // Move a whole character - a surrogate pair is one character.
    const code = text.codePointAt(index);
    index += code != null && code > 0xffff ? 2 : 1;
  }
  if (plainStart < text.length) {
    segments.push({ text: text.slice(plainStart) });
  }
  return segments;
}

/**
 * Where a term was found: the file and the place in it.
 *
 * "《采购合同.md》第 12 页". A term the knowledge base does not hold
 * says so instead. Nothing here explains the term - the passages are
 * the evidence, and an explanation would not be (ADR-041).
 */
export function termContextSummary(context: TermContext): string {
  const term = (context?.term ?? "").trim();
  const items = context?.contexts ?? [];
  if (!context?.found || items.length === 0) {
    return term ? `知识库里没有「${term}」` : "知识库里没有这个术语";
  }
  const places = items.map(placeOf).filter((place) => place.length > 0);
  if (places.length === 0) {
    return term
      ? `知识库里有「${term}」，但没有可定位的原文`
      : "知识库里有这个术语，但没有可定位的原文";
  }
  return places.join("、");
}

/**
 * The terms to look for, the longest first.
 *
 * The order matters: the longest term wins over the shorter ones
 * inside it, so "采购合同" is marked as a whole and the "合同" in it
 * is not marked a second time.
 */
function wantedTerms(terms: string[]): string[] {
  const seen = new Set<string>();
  const wanted: string[] = [];
  for (const raw of terms ?? []) {
    const term = (raw ?? "").trim();
    if (!term || seen.has(term)) continue;
    seen.add(term);
    wanted.push(term);
  }
  wanted.sort((a, b) => b.length - a.length);
  return wanted;
}

/** The longest term that starts at this place, if there is one. */
function longestAt(
  text: string,
  index: number,
  wanted: string[]
): { term: string; length: number } | null {
  for (const term of wanted) {
    if (text.startsWith(term, index)) return { term, length: term.length };
  }
  return null;
}

/** One passage, and where it was read from. */
function placeOf(item: TermContextItem): string {
  const name = (item.file_name ?? "").trim();
  const location = (item.location ?? "").trim() || pageOf(item.page);
  if (name && location) return `《${name}》${location}`;
  if (name) return `《${name}》`;
  return location;
}

/** The page a passage is on - only used when the backend sent no place. */
function pageOf(page: number | null | undefined): string {
  return page != null && Number.isFinite(page) ? `第 ${page} 页` : "";
}
