#!/usr/bin/env bash
# The fast pre-commit checks: lint, format and types. The full suite and the coverage gate run
# in CI (and by hand: `uv run pytest --cov`); they are too slow to run on every commit.
#
# The checks cover the working tree, not just what is staged, so unstaged edits can fail a
# commit whose staged content is clean. The hooks are best-effort; CI is the gate.
#
# Both the git pre-commit hook and the Claude Code hook in .claude/settings.json call this, so a
# commit made through Claude Code runs it twice (under a second). That is deliberate: if either
# hook skipped when the other is installed, a commit that bypasses the git hook would skip both.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if ! command -v uv >/dev/null 2>&1; then
  echo "check: uv not found (https://docs.astral.sh/uv/); cannot run the checks" >&2
  exit 1
fi

run() {
  echo "check: $*" >&2
  uv run --locked --group dev --quiet "$@" >&2 || { echo "check: '$*' failed: fix the findings above before committing" >&2; exit 1; }
}

run ruff check .
run ruff format --check .
run mypy
