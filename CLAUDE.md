# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hermetic is an open-source agentic tester for REST microservices: given an OpenAPI spec and design docs, it infers the service's dependencies, provisions mocks, generates and runs a pytest/httpx suite, and records each run. Status is pre-alpha (milestone M0): only the package skeleton and a `hermetic version` CLI stub exist. The pipeline in `docs/architecture.md` is the target design, not current behavior; check the code before assuming a stage exists.

## Commands

Uses [uv](https://docs.astral.sh/uv/); Python 3.12+.

```bash
uv sync --group dev
uv run pytest --cov                              # full suite; fails under 100% line+branch coverage
uv run pytest tests/unit/test_cli.py::test_version_prints_package_version   # single test
uv run pytest -m live                            # opt-in tests that call the real Claude API
uv run ruff check . && uv run ruff format --check .
uv run mypy                                      # strict, covers src and tests
docker build -f docker/Dockerfile -t hermetic:dev . && docker run --rm hermetic:dev version
```

CI (`.github/workflows/ci.yml`) runs the lint, mypy and pytest-with-coverage steps above on Python 3.12 and 3.13, plus a Docker build (without pushing). `release.yml` reuses it as a gate before publishing.

## Architecture decisions that span files

- **Deterministic code vs agent.** Parsing, diffing, execution, run recording and reporting are plain code. The Claude Agent SDK is used only for dependency inference and test authoring, and must sit behind an interface so unit tests use a fake agent. That is how the 100% coverage gate stays reachable without API calls.
- **Run history** (layout and fields in `docs/architecture.md`): the CLI and the web UI must depend only on the `RunStore` protocol, never on `FileRunStore`, so a database store can replace it.
- **Two `.hermetic` directories mean different things.** `.hermetic/runs/` is local history (gitignored); `tests/.hermetic/manifest.json` is the test manifest and is committed.
- **Compose allowlist.** Provisioning mounts the host Docker socket (root-equivalent), so every agent-generated compose file must pass deterministic validation before it runs. The rejected constructs are listed in `docs/architecture.md` (Safety).

## Repo conventions

- `docs/architecture.md` is the committed design doc. `docs/PLAN.md` is a local, uncommitted working plan (excluded via `.git/info/exclude`); never commit it. `CHANGELOG.md` is intentionally absent until the initial planned work is done.
- PRs are reviewed by the `code-check` GitHub workflow in full mode (every reviewer, including security and performance). It needs the `CLAUDE_CODE_OAUTH_TOKEN` repository secret and silently skips without it.
- `.github/workflows/code-check-pin.yml` opens draft PRs that bump `code-check-ref`; it needs the `CI_PIN_BOT_TOKEN` secret (scopes are listed in the workflow).
- Releases are tag-driven (`v*`); see `.github/workflows/release.yml`.
