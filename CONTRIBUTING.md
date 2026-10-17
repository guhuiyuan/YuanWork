# Contributing to YuanWork

Thank you for your interest in YuanWork! This is a personal-scale project but contributions are welcome.

## Development Setup

### Prerequisites

- Python 3.10+
- Node.js 18+
- Rust 1.70+ (for Tauri shell)
- [uv](https://github.com/astral-sh/uv) (Python package manager)

### Backend

```bash
cd src/backend
uv sync
cp .env.example .env
# Edit .env to add your LLM API key
uv run python -m yuanwork --port 8765
```

### Frontend (Tauri)

```bash
cd src/app
npm install
npm run tauri dev
```

### Tests

```bash
cd src/backend
uv run pytest
```

## Code Style

### Python

- **Formatter**: `ruff format`
- **Linter**: `ruff check`
- **Type checker**: `mypy` (best-effort, not enforced)
- Use `pathlib.Path` not `os.path`
- Use Pydantic for all data models
- Docstrings: PEP 257, but terse (1-2 lines)

### TypeScript / React

- **Formatter**: `prettier`
- **Linter**: `eslint` (with `@typescript-eslint`)
- Functional components + hooks
- Zustand for global state
- Use `interface` over `type` for object shapes

### Rust

- `cargo fmt`
- `cargo clippy`
- No `unwrap()` in production code; use `?` and propagate errors
- Errors via `thiserror`

## Architecture Guidelines

1. **Privacy first** — never log raw document text, never send full documents to the cloud LLM
2. **Mandatory citations** — high-confidence answers must always include source citations
3. **Refuse when uncertain** — low confidence = refuse + suggest likely location
4. **KB primary** — when LLM training knowledge conflicts with KB, KB wins + warn user
5. **Local by default** — minimize network calls; provide offline mode

## Pull Request Process

1. Open an issue first for non-trivial changes
2. Fork and create a feature branch
3. Add tests for new behavior
4. Run `uv run pytest` and `cargo test` before pushing
5. Keep PRs focused (one concern per PR)
6. Update docs if you change user-facing behavior

## Commit Message Format

```
<scope>: <imperative summary>

<optional body explaining why>

Co-Authored-By: Claude Code <noreply@anthropic.com>
```

Examples:
- `backend: add conflict detection prompt template`
- `app: add sidebar navigation for wiki view`
- `docs: explain 3-step reasoning architecture`

## License

By contributing, you agree your contributions will be licensed under Apache 2.0.
