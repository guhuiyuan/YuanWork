/**
 * The position a citation points at.
 *
 * The backend sends the position twice: as an anchor (a kind and the
 * coordinates that kind defines) and as the flat fields the older
 * shape used. The anchor is what the shell needs; the flat fields are
 * what the older backends send, so both are read here.
 */

import type { Citation, CitationAnchor } from "../store.ts";

/**
 * The anchor of a citation, or null when the citation points nowhere.
 *
 * A citation without a position cannot be opened at a place - it can
 * only be opened.
 */
export function anchorOf(citation: Citation): CitationAnchor | null {
  const anchor = citation.anchor;
  if (anchor && anchor.kind) return anchor;

  if (citation.page != null) return { kind: "page", page: citation.page };
  if (citation.slide_number != null)
    return { kind: "slide", slide: citation.slide_number };
  if (citation.sheet) {
    return {
      kind: "sheet_row",
      sheet: citation.sheet,
      row: citation.row ?? undefined,
    };
  }
  if (citation.section) return { kind: "section", section: citation.section };
  return null;
}

/**
 * The position in words - what the reader sees.
 *
 * "第 12 页", "Slide 3", "进度表 第 7 行".
 */
export function describeAnchor(anchor: CitationAnchor | null): string {
  if (!anchor) return "";
  switch (anchor.kind) {
    case "page":
      return anchor.page != null ? `第 ${anchor.page} 页` : "";
    case "slide":
      return anchor.slide != null ? `Slide ${anchor.slide}` : "";
    case "section":
      return anchor.section ?? "";
    case "sheet_row":
      if (anchor.sheet && anchor.row != null)
        return `${anchor.sheet} 第 ${anchor.row} 行`;
      if (anchor.sheet) return anchor.sheet;
      return anchor.row != null ? `第 ${anchor.row} 行` : "";
    case "heading_path":
      return (anchor.heading_path ?? []).join(" › ");
    default:
      return "";
  }
}

/**
 * The whole label of a citation: the file and where in it.
 *
 * The file name comes first - that is what the reader scans for - and
 * the position says where to look inside it.
 */
export function citationLabel(citation: Citation): string {
  const name = citation.file_name || "（未知文件）";
  const location = citation.location || describeAnchor(anchorOf(citation));
  return location ? `${name} · ${location}` : name;
}
