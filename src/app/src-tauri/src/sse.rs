//! Minimal Server-Sent Events frame parser.
//!
//! The Python backend streams with `sse-starlette`, which encodes a frame
//! as
//!
//! ```text
//! event: <type>\r\ndata: <payload>\r\n\r\n
//! ```
//!
//! Note the CRLF - not LF - line endings, and the `event: ping` keepalive
//! frame the server sends periodically.
//!
//! The parser is incremental: network chunks arrive at arbitrary
//! boundaries, so a frame can be split across two `push()` calls, and a
//! multi-byte UTF-8 character can be split across two byte chunks. Callers
//! should decode bytes to a string with a streaming decoder before feeding
//! them in.

/// A single decoded SSE frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SseEvent {
    /// The `event:` field. Defaults to `"message"` when the frame has none.
    pub event: String,
    /// The `data:` field. Multiple `data:` lines are joined with `\n`.
    pub data: String,
}

/// Incremental SSE frame parser.
#[derive(Debug, Default)]
pub struct SseParser {
    buffer: String,
}

impl SseParser {
    /// Create an empty parser.
    pub fn new() -> Self {
        Self {
            buffer: String::new(),
        }
    }

    /// Feed a chunk of the raw stream and return every complete frame it
    /// produced. Any trailing partial frame stays buffered for the next call.
    pub fn push(&mut self, chunk: &str) -> Vec<SseEvent> {
        self.buffer.push_str(chunk);

        let mut events = Vec::new();
        while let Some((frame, rest)) = split_frame(&self.buffer) {
            self.buffer = rest;
            if let Some(event) = parse_frame(&frame) {
                events.push(event);
            }
        }
        events
    }

    /// Flush a trailing frame that was never terminated by a blank line.
    ///
    /// Call this once the stream ends; servers that close the connection
    /// without a final blank line would otherwise lose that frame.
    pub fn finish(&mut self) -> Vec<SseEvent> {
        let rest = std::mem::take(&mut self.buffer);
        match parse_frame(&rest) {
            Some(event) => vec![event],
            None => Vec::new(),
        }
    }
}

/// Split off the first complete frame, returning it and the remaining text.
///
/// Accepts both CRLF (`\r\n\r\n`) and LF (`\n\n`) frame terminators.
fn split_frame(buffer: &str) -> Option<(String, String)> {
    let crlf = buffer.find("\r\n\r\n");
    let lf = buffer.find("\n\n");

    match (crlf, lf) {
        (Some(c), Some(l)) if l < c => Some((buffer[..l].to_string(), buffer[l + 2..].to_string())),
        (Some(c), _) => Some((buffer[..c].to_string(), buffer[c + 4..].to_string())),
        (None, Some(l)) => Some((buffer[..l].to_string(), buffer[l + 2..].to_string())),
        (None, None) => None,
    }
}

/// Parse the body of one frame (the text between two blank lines).
fn parse_frame(frame: &str) -> Option<SseEvent> {
    let mut event = String::new();
    let mut data_lines: Vec<String> = Vec::new();

    for raw_line in frame.split('\n') {
        let line = raw_line.strip_suffix('\r').unwrap_or(raw_line);

        // Blank line or comment (used by the server's keepalive).
        if line.is_empty() || line.starts_with(':') {
            continue;
        }

        let (field, value) = match line.split_once(':') {
            Some((f, v)) => (f, v),
            // A line without a colon is a field name with an empty value.
            None => (line, ""),
        };
        // Exactly one leading space after the colon is part of the framing.
        let value = value.strip_prefix(' ').unwrap_or(value);

        match field {
            "event" => event = value.to_string(),
            "data" => data_lines.push(value.to_string()),
            _ => {}
        }
    }

    if event.is_empty() && data_lines.is_empty() {
        return None;
    }
    if event.is_empty() {
        event = "message".to_string();
    }

    Some(SseEvent {
        event,
        data: data_lines.join("\n"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_crlf_frame() {
        let mut p = SseParser::new();
        let events = p.push("event: token\r\ndata: {\"text\":\"hi\"}\r\n\r\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "token");
        assert_eq!(events[0].data, "{\"text\":\"hi\"}");
    }

    #[test]
    fn parses_lf_frame() {
        let mut p = SseParser::new();
        let events = p.push("event: token\ndata: hi\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "token");
        assert_eq!(events[0].data, "hi");
    }

    #[test]
    fn frame_split_across_pushes() {
        let mut p = SseParser::new();
        assert!(p.push("event: tok").is_empty());
        assert!(p.push("en\r\ndata: hel").is_empty());
        let events = p.push("lo\r\n\r\n").clone();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "token");
        assert_eq!(events[0].data, "hello");
    }

    #[test]
    fn ignores_comment_lines() {
        let mut p = SseParser::new();
        let events = p.push(": keepalive\r\nevent: token\r\ndata: x\r\n\r\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "token");
    }

    #[test]
    fn joins_multiline_data() {
        let mut p = SseParser::new();
        let events = p.push("event: token\r\ndata: a\r\ndata: b\r\n\r\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].data, "a\nb");
    }

    #[test]
    fn finish_flushes_unterminated_frame() {
        let mut p = SseParser::new();
        assert!(p.push("event: token\r\ndata: x\r\n").is_empty());
        let events = p.finish();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "token");
        assert_eq!(events[0].data, "x");
    }

    #[test]
    fn defaults_event_name_to_message() {
        let mut p = SseParser::new();
        let events = p.push("data: bare\r\n\r\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "message");
        assert_eq!(events[0].data, "bare");
    }

    #[test]
    fn parses_several_frames_in_one_chunk() {
        let mut p = SseParser::new();
        let events = p.push(
            "event: token\r\ndata: a\r\n\r\nevent: done\r\ndata: {}\r\n\r\n",
        );
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].event, "token");
        assert_eq!(events[1].event, "done");
    }

    #[test]
    fn ping_frame_is_parsed() {
        let mut p = SseParser::new();
        let events = p.push("event: ping\r\ndata: \r\n\r\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "ping");
        assert_eq!(events[0].data, "");
    }
}
