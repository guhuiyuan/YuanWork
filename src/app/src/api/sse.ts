/**
 * Server-Sent Events parsing, browser mode.
 *
 * Kept in its own module (no React, no Tauri imports) so it can be unit
 * tested directly with `node --test`.
 *
 * The backend uses sse-starlette, which terminates frames with CRLF
 * (`\r\n\r\n`); bare LF is accepted too.
 */

/** One raw frame: the `event:` name and the joined `data:` payload. */
export interface RawSseFrame {
  event: string;
  data: string;
}

/**
 * Split off every complete frame in `buffer`.
 *
 * Whatever follows the last complete frame is returned as `rest` so it can
 * be prepended to the next chunk.
 */
export function parseSseFrames(buffer: string): {
  frames: RawSseFrame[];
  rest: string;
} {
  const frames: RawSseFrame[] = [];
  let rest = buffer;

  for (;;) {
    const crlf = rest.indexOf("\r\n\r\n");
    const lf = rest.indexOf("\n\n");
    let end: number;
    if (crlf >= 0 && (lf < 0 || crlf < lf)) {
      end = crlf + 4;
    } else if (lf >= 0) {
      end = lf + 2;
    } else {
      break;
    }
    const parsed = parseSseFrame(rest.slice(0, end));
    rest = rest.slice(end);
    if (parsed) frames.push(parsed);
  }

  return { frames, rest };
}

/** Parse the body of one frame (the text between two blank lines). */
export function parseSseFrame(frame: string): RawSseFrame | null {
  let event = "";
  const dataLines: string[] = [];

  for (const rawLine of frame.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    // Blank line, or a comment (the server's keepalive).
    if (!line || line.startsWith(":")) continue;

    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    // Exactly one leading space after the colon is part of the framing.
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (!event && dataLines.length === 0) return null;
  return { event: event || "message", data: dataLines.join("\n") };
}
