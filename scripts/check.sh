#!/usr/bin/env bash
# The fast pre-commit checks: lint, format and types. The full suite and the coverage gate run
# in CI (and by hand: `uv run pytest --cov`); they are too slow to run on every commit.
#
# `--if-no-git-hook` makes this exit at once when the git hook is installed, so a commit made
# through Claude Code does not run the checks twice (once per hook).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if [ "${1:-}" = "--if-no-git-hook" ] && [ "$(git config core.hooksPath 2>/dev/null)" = ".githooks" ]; then
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "check: uv not found (https://docs.astral.sh/uv/); skipping checks" >&2
  exit 0
fi

run() {
  echo "check: $*" >&2
  uv run --group dev --quiet "$@" >&2 || { echo "check: '$*' failed: fix the findings above before committing" >&2; exit 1; }
}

run ruff check .
run ruff format --check .
run mypy
