/**
 * The terms of an answer, in the shape the answer is rendered in.
 *
 * The answer is markdown, so its text arrives in pieces: a paragraph
 * here, a bold run there, a list item somewhere else. A term can sit
 * in any of them - and it has to stay clickable wherever it sits
 * (AC-F-030): the reader opens a term and sees it in the original.
 *
 * So the terms are put back in **after** the markdown is parsed, on the
 * tree of nodes - not in the markdown source. In the source a term
 * inside a code block would come out as a link, and the code would
 * show the link instead of the word.
 *
 * Everything here is pure and needs no browser: node --test can call it.
 */

import {
  Children,
  cloneElement,
  createElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";

import { markTerms } from "./terms.ts";

/** The class the term buttons carry - the one the plain text had. */
export const TERM_CLASS = "msg__term";

/**
 * Put the terms back in as buttons, wherever the text sits.
 *
 * The text that goes in is the text that comes out: the buttons carry
 * the term itself, so nothing is added and nothing is lost.
 */
export function markTermsInNodes(
  children: ReactNode,
  terms: string[],
  onTerm: (term: string) => void
): ReactNode {
  if (!terms || terms.length === 0) return children;
  return Children.map(children, (child) => {
    if (typeof child === "string") {
      return markTerms(child, terms).map((segment, index) =>
        segment.term
          ? createElement(
              "button",
              {
                key: index,
                type: "button",
                className: TERM_CLASS,
                title: "查看原文上下文",
                onClick: () => onTerm(segment.term as string),
              },
              segment.text
            )
          : createElement("span", { key: index }, segment.text)
      );
    }
    if (isValidElement(child)) {
      const element = child as ReactElement<{ children?: ReactNode }>;
      const inner = element.props?.children;
      // A node without text in it (an image, a line break) is left be.
      if (inner === undefined || inner === null) return child;
      return cloneElement(
        element,
        undefined,
        markTermsInNodes(inner, terms, onTerm)
      );
    }
    return child;
  });
}

/**
 * The text of a piece of rendered markdown.
 *
 * Used to tell whether a link's label already is its address - a
 * link whose label is the URL must not show the URL twice.
 */
export function textOf(children: ReactNode): string {
  if (typeof children === "string") return children;
  if (typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textOf).join("");
  if (isValidElement(children)) {
    const props = children.props as { children?: ReactNode } | undefined;
    return props?.children ? textOf(props.children) : "";
  }
  return "";
}

/**
 * The first words of an answer, without the marks.
 *
 * For the preview in the trace panel: it shows the exchange as the
 * reader sees it, and the reader does not see "##" or "**".
 *
 * Only the marks that pair up are taken off. A single star is left
 * alone - "A*算法" is a name, not an emphasis mark.
 */
export function plainPreview(markdown: string, limit = 100): string {
  const text = (markdown ?? "")
    .replace(/^[ \t]*#{1,6}[ \t]+/gm, "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/\n{2,}/g, "\n")
    .trim();
  return text.slice(0, limit);
}
