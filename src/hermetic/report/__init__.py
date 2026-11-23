"""Report stage: curl repros and the sinks that publish a finished run."""

from hermetic.report.curl import build_curl
from hermetic.report.html_sink import HtmlSink
from hermetic.report.json_sink import JsonSink
from hermetic.report.markdown import MarkdownSink, StdoutSink
from hermetic.report.sinks import (
    ReportSink,
    SinkContext,
    SinkError,
    SinkFactory,
    load_sinks,
)

BUILTIN_SINKS: dict[str, SinkFactory] = {
    "markdown": MarkdownSink,
    "html": HtmlSink,
    "json": JsonSink,
    "stdout": StdoutSink,
}

__all__ = [
    "BUILTIN_SINKS",
    "ReportSink",
    "SinkContext",
    "SinkError",
    "SinkFactory",
    "build_curl",
    "load_sinks",
]
