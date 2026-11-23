"""HTML report: one self-contained file, no scripts. All recorded text is escaped."""

from __future__ import annotations

from html import escape

from hermetic.history.models import Exchange, RunRecord, summarize
from hermetic.report.sinks import SinkContext

_STYLE = (
    "body{font:14px system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}"
    "pre{background:#f4f4f4;padding:.5rem;overflow:auto;white-space:pre-wrap}"
    ".failed,.error{color:#b00020}.passed{color:#1b7f3b}"
)


def _exchange(exchange: Exchange) -> str:
    status = "no response" if exchange.status_code is None else str(exchange.status_code)
    body = exchange.response_body or ""
    return (
        f"<p>{escape(exchange.method)} {escape(exchange.url)} &rarr; {escape(status)}</p>"
        f"<pre>{escape(body)}</pre>"
    )


def render_html(record: RunRecord) -> str:
    summary = summarize(record)
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>Hermetic run {escape(record.run_id)}</title><style>{_STYLE}</style></head><body>",
        f"<h1>Hermetic run {escape(record.run_id)}</h1>",
        f"<p>Target: {escape(record.target)}<br>Started: {escape(record.started_at)}</p>",
        f"<p>Passed {summary.passed}, failed {summary.failed}, errors {summary.errors}, "
        f"skipped {summary.skipped}</p>",
    ]
    broken = [r for r in record.results if r.status in ("failed", "error")]
    if broken:
        parts.append("<h2>Failures</h2>")
    for result in broken:
        parts.append(
            f"<h3>{escape(result.test_id)} <span class='{result.status}'>"
            f"{escape(result.status)}</span></h3>"
        )
        if result.message:
            parts.append(f"<pre>{escape(result.message)}</pre>")
        if result.curl:
            parts.append(f"<p>Repro:</p><pre>{escape(result.curl)}</pre>")
        parts += [_exchange(e) for e in result.exchanges[-1:]]
    if record.changes:
        parts.append("<h2>Suite changes</h2><ul>")
        parts += [
            f"<li>{escape(c.kind)}: {escape(c.test_id)}: {escape(c.reason)}</li>"
            for c in record.changes
        ]
        parts.append("</ul>")
    parts.append("</body></html>")
    return "".join(parts)


class HtmlSink:
    def __init__(self, context: SinkContext) -> None:
        self._dir = context.output_dir

    def publish(self, record: RunRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{record.run_id}.html").write_text(render_html(record), encoding="utf-8")
