# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hermetic is an open-source agentic tester for REST microservices: given an OpenAPI spec and design docs, it infers the service's dependencies, provisions mocks, generates and runs a pytest/httpx suite, and records each run. Status is early (v0.2): every stage of the pipeline in `docs/architecture.md` exists, and its Known limits section lists what does not. Check `src/hermetic` and `hermetic --help` before assuming a command behaves as described.

## Commands

Uses [uv](https://docs.astral.sh/uv/); Python 3.12+.

```bash
uv sync --group dev
uv run pytest --cov                              # full suite; fails under 100% line+branch coverage
uv run pytest tests/unit/test_cli.py::test_version_prints_package_version   # single test
uv run pytest -m live                            # opt-in tests that call the real Claude API
uv run pytest -m integration                     # opt-in tests that start real containers (needs Docker)
uv run ruff check . && uv run ruff format --check .
uv run mypy                                      # strict, covers src and tests
docker build -f docker/Dockerfile -t hermetic:dev . && docker run --rm hermetic:dev version
```

**Commit checks.** `scripts/check.sh` (lint, format and types, no tests) runs from `.githooks/pre-commit` and from the Claude hook in `.claude/settings.json`. `scripts/install-git-hooks.sh` sets the repo-wide `core.hooksPath`, so it is a deliberate user step that nothing here runs. Both hooks are best-effort (the Claude hook matches only commands that start with `git commit`, and the git hook can be bypassed); CI is the gate.

CI (`.github/workflows/ci.yml`) runs the lint, mypy and pytest-with-coverage steps above on Python 3.12 and 3.13, the `integration` tests against the runner's Docker, plus a Docker build (without pushing). `release.yml` reuses it as a gate before publishing. `security.yml` holds the security scans (its jobs say what each does); keep `zizmor --min-severity medium .github/workflows` clean (checkouts set `persist-credentials: false`, and `setup-uv` in the release build and the security jobs sets `enable-cache: false`).

## Architecture decisions that span files

- **Deterministic code vs agent.** Parsing, diffing, execution, run recording and reporting are plain code. The Claude Agent SDK is used only for dependency inference and test authoring, and must sit behind an interface so unit tests use a fake agent. That is how the 100% coverage gate stays reachable without API calls.
- **Run history** (layout and fields in `docs/architecture.md`): the CLI and the web UI must depend only on the `RunStore` protocol, never on `FileRunStore`, so a database store can replace it.
- **Two `.hermetic` directories mean different things.** `.hermetic/runs/` is local history (gitignored); `tests/.hermetic/manifest.json` is the test manifest and is committed.
- **Compose allowlist.** Provisioning mounts the host Docker socket (root-equivalent), so every agent-generated compose file must pass deterministic validation before it runs. The rejected constructs are listed in `docs/architecture.md` (Safety).

## Repo conventions

- `docs/architecture.md` is the committed design doc. `docs/PLAN.md` is a local, uncommitted working plan (excluded via `.git/info/exclude`); never commit it. `CHANGELOG.md` is intentionally absent until the initial planned work is done.
- PRs are reviewed by the `code-check` GitHub workflow in full mode (every reviewer, including security and performance); draft PRs are skipped. It needs the `CLAUDE_CODE_OAUTH_TOKEN` repository secret and silently skips without it.
- `.github/workflows/code-check-pin.yml` opens draft PRs that bump `code-check-ref`; it needs the `GH_TOKEN_PERSONAL` secret (scopes are listed in the workflow).
- Releases are tag-driven (`v*`); see `.github/workflows/release.yml`.
