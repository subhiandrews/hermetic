#!/usr/bin/env bash
# Point this repository's git at the checked-in hooks in .githooks.
# core.hooksPath is repository-level config: it applies to every worktree.
set -eu

if ! ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "install-git-hooks: not inside a git repository" >&2
  exit 1
fi
cd "$ROOT"
prev="$(git config --get core.hooksPath || true)"
if [ -n "$prev" ] && [ "$prev" != ".githooks" ]; then
  # Once core.hooksPath is set, git ignores every hook in .git/hooks.
  echo "install-git-hooks: core.hooksPath is already set to $prev; not changing it" >&2
  exit 1
fi
git config core.hooksPath .githooks
echo "set core.hooksPath=.githooks in $ROOT (applies to all of its worktrees)"
