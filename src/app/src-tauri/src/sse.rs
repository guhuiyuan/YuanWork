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
//! multi-byte UTF-8 character can be split across two byte chunks. Use
//! [`Utf8Decoder`] to turn the raw bytes into text before feeding them
//! to [`SseParser::push`].

/// A single decoded SSE frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SseEvent {
    /// The `event:` field. Defaults to `"message"` when the frame has none.
    pub event: String,
    /// The `data:` field. Multiple `data:` lines are joined with `\n`.
    pub data: String,
}

/// Incremental UTF-8 decoder.
///
/// Network chunks can split a multi-byte character in half. Decoding each
/// chunk on its own would turn the split character into U+FFFD, silently
/// corrupting the text (e.g. "违约金" arriving as "��约金"). This decoder
/// holds back an incomplete trailing character until the rest of its bytes
/// arrive.
#[derive(Debug, Default)]
pub struct Utf8Decoder {
    pending: Vec<u8>,
}

impl Utf8Decoder {
    /// Create an empty decoder.
    pub fn new() -> Self {
        Self { pending: Vec::new() }
    }

    /// Decode as much of `bytes` as possible.
    ///
    /// Returns the decoded text. If `bytes` ends in the middle of a
    /// multi-byte character, that character is held back and returned by
    /// the next call (or by [`Utf8Decoder::finish`]).
    pub fn decode(&mut self, bytes: &[u8]) -> String {
        self.pending.extend_from_slice(bytes);
        let mut out = String::new();

        loop {
            match std::str::from_utf8(&self.pending) {
                Ok(text) => {
                    out.push_str(text);
                    self.pending.clear();
                    break;
                }
                Err(err) => {
                    let valid = err.valid_up_to();
                    // `valid_up_to` guarantees this prefix is valid UTF-8.
                    if let Ok(prefix) = std::str::from_utf8(&self.pending[..valid]) {
                        out.push_str(prefix);
                    }
                    match err.error_len() {
                        // Incomplete trailing character: wait for more bytes.
                        None => {
                            self.pending.drain(..valid);
                            break;
                        }
                        // Genuinely invalid bytes: one replacement char for
                        // the offending sequence, then keep going.
                        Some(len) => {
                            out.push('\u{FFFD}');
                            self.pending.drain(..valid + len);
                        }
                    }
                }
            }
        }

        out
    }

    /// Flush bytes that never formed a complete character.
    ///
    /// Call this once the stream ends; a truncated trailing character
    /// becomes U+FFFD instead of being silently dropped.
    pub fn finish(&mut self) -> String {
        if self.pending.is_empty() {
            return String::new();
        }
        let text = String::from_utf8_lossy(&self.pending).into_owned();
        self.pending.clear();
        text
    }
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

    // ===== Utf8Decoder =====

    #[test]
    fn decoder_passes_plain_text_through() {
        let mut d = Utf8Decoder::new();
        assert_eq!(d.decode(b"hello"), "hello");
        assert_eq!(d.decode(b" world"), " world");
        assert_eq!(d.finish(), "");
    }

    #[test]
    fn decoder_keeps_split_character_intact() {
        // "违" is three bytes. Split it 2 + 1 across two chunks.
        let text = "违约金";
        let bytes = text.as_bytes();
        let mut d = Utf8Decoder::new();
        let first = d.decode(&bytes[..2]);
        assert_eq!(first, "", "the partial character must be held back");
        let second = d.decode(&bytes[2..]);
        assert_eq!(format!("{first}{second}"), text);
    }

    #[test]
    fn decoder_survives_a_split_inside_a_sse_frame() {
        // The realistic case: a frame boundary lands inside a character.
        let payload = "event: token\r\ndata: {\"text\":\"违约金\"}\r\n\r\n";
        let bytes = payload.as_bytes();
        // Cut in the middle of the first multi-byte character.
        let cut = bytes
            .iter()
            .position(|b| *b == 0xE8)
            .map(|i| i + 1)
            .unwrap();
        let mut decoder = Utf8Decoder::new();
        let mut parser = SseParser::new();

        let mut events = parser.push(&decoder.decode(&bytes[..cut]));
        assert!(events.is_empty(), "the frame is not complete yet");
        events = parser.push(&decoder.decode(&bytes[cut..]));
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "token");
        assert!(events[0].data.contains("违约金"), "got {}", events[0].data);
    }

    #[test]
    fn decoder_replaces_invalid_bytes() {
        let mut d = Utf8Decoder::new();
        // 0xFF is not a valid UTF-8 byte.
        assert_eq!(d.decode(&[0xFF, 0x41]), "\u{FFFD}A");
    }

    #[test]
    fn decoder_flushes_a_truncated_character() {
        let mut d = Utf8Decoder::new();
        let bytes = "违".as_bytes();
        assert_eq!(d.decode(&bytes[..2]), "");
        assert_eq!(d.finish(), "\u{FFFD}");
    }
}
