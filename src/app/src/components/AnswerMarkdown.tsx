/**
 * The answer, rendered.
 *
 * The answer is markdown: the model writes it that way, and the refusal
 * template is written that way. Showing it as plain text showed the
 * reader the marks themselves - "## 你可以：" and "**补上另一份文档**"
 * (TASK-086).
 *
 * Three things must not change:
 *
 * - **the terms stay clickable** (AC-F-030): the reader opens a term
 *   and sees it in the original
 * - **the text is not changed**: what is rendered is what was written
 * - **a link never takes the application away**: an answer can point at
 *   a web page, and clicking it must not navigate the window the work
 *   is in - so the address is shown, not followed
 */

import { useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { markTermsInNodes, textOf } from "../api/markdown.ts";

interface Props {
  /** The answer, in markdown. */
  content: string;
  /** The terms in it - each one opens the text it stands for. */
  terms: string[];
  /** Called with the term the reader clicked. */
  onTerm: (term: string) => void;
}

export function AnswerMarkdown({ content, terms, onTerm }: Props) {
  const components = useMemo<Components>(
    () => buildComponents(terms, onTerm),
    [terms, onTerm]
  );

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {content}
    </ReactMarkdown>
  );
}

/**
 * The markdown elements, with the terms put back in.
 *
 * Only the elements that hold text are touched - a code block keeps
 * its code, and an image is not fetched (the answer is the reader's
 * own material; an image would be a request to somewhere else).
 */
function buildComponents(
  terms: string[],
  onTerm: (term: string) => void
): Components {
  const marked = (children: ReactNode) =>
    markTermsInNodes(children, terms, onTerm);

  return {
    p: ({ children }) => <p>{marked(children)}</p>,
    h1: ({ children }) => <h1>{marked(children)}</h1>,
    h2: ({ children }) => <h2>{marked(children)}</h2>,
    h3: ({ children }) => <h3>{marked(children)}</h3>,
    h4: ({ children }) => <h4>{marked(children)}</h4>,
    li: ({ children }) => <li>{marked(children)}</li>,
    td: ({ children }) => <td>{marked(children)}</td>,
    th: ({ children }) => <th>{marked(children)}</th>,
    blockquote: ({ children }) => <blockquote>{marked(children)}</blockquote>,
    a: ({ href, children }) => {
      const label = textOf(children);
      // A link whose label is its address would show the address twice.
      const showAddress = !!href && !label.includes(href);
      return (
        <span className="md__link" title={href}>
          {children}
          {showAddress && <span className="md__link-url">（{href}）</span>}
        </span>
      );
    },
    img: ({ alt }) => (
      <span className="md__image">{alt || "（图片）"}</span>
    ),
  };
}
