"""Markdown report, written to a file and printed by the stdout sink."""

from __future__ import annotations

import re

import typer

from hermetic.history.models import RunRecord, summarize
from hermetic.report.sinks import SinkContext


def _fence(text: str) -> str:
    """Wrap text in a code fence longer than any backtick run inside it."""
    longest = current = 0
    for char in text:
        current = current + 1 if char == "`" else 0
        longest = max(longest, current)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


def render_markdown(record: RunRecord) -> str:
    summary = summarize(record)
    lines = [
        f"# Hermetic run {record.run_id}",
        "",
        f"- Target: {record.target}",
        f"- Started: {record.started_at}",
        f"- Passed: {summary.passed}, failed: {summary.failed}, errors: {summary.errors}, "
        f"skipped: {summary.skipped}",
    ]
    if record.changes:
        lines.append(
            f"- Suite changes: {summary.added} added, {summary.modified} modified, "
            f"{summary.dropped} dropped"
        )
    broken = [r for r in record.results if r.status in ("failed", "error")]
    if broken:
        lines += ["", "## Failures"]
        for result in broken:
            lines += ["", f"### {result.test_id} ({result.status})"]
            if result.operation_id:
                lines.append(f"Operation: `{result.operation_id}`")
            if result.message:
                lines += ["", _fence(result.message)]
            if result.curl:
                lines += ["", "Repro:", "", _fence(result.curl)]
    if record.changes:
        lines += ["", "## Suite changes"]
        lines += [f"- {c.kind}: {c.test_id}: {c.reason}" for c in record.changes]
    return "\n".join(lines) + "\n"


class MarkdownSink:
    def __init__(self, context: SinkContext) -> None:
        self._dir = context.output_dir

    def publish(self, record: RunRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{record.run_id}.md").write_text(render_markdown(record), encoding="utf-8")


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _printable(text: str) -> str:
    """Escape control characters (ANSI, OSC) so target-controlled text cannot drive a terminal."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


class StdoutSink:
    def __init__(self, context: SinkContext) -> None:
        del context

    def publish(self, record: RunRecord) -> None:
        typer.echo(_printable(render_markdown(record)))
