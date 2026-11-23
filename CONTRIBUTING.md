# Contributing to Hermetic

Thanks for helping. Hermetic is MIT-licensed; by contributing you agree your contribution is released under the same license.

## Workflow

1. Open an issue to discuss larger changes first.
2. Fork, branch from `main`, and keep each pull request focused.
3. Run the checks locally before pushing:
   ```bash
   uv sync --group dev
   uv run ruff check . && uv run ruff format --check .
   uv run mypy
   uv run pytest --cov
   ```
4. Open a pull request. CI runs lint, type checks, tests, and a Docker build.

## Standards

- Python 3.12+, fully typed (`mypy --strict`), formatted with `ruff`.
- Deterministic code (parsing, diffing, execution, reporting, history) must keep 100% line and branch coverage.
- Code that calls the Claude API goes behind an interface so tests can use a fake agent. Tests that need the real API are marked `live` and are opt-in.
- Never commit secrets or run history (`.hermetic/runs/` is gitignored).

## Design

Read `docs/architecture.md` before changing how the pipeline fits together.
