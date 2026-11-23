"""JSON sink: the full run record, as stored."""

from __future__ import annotations

from hermetic.history.models import RunRecord
from hermetic.report.sinks import SinkContext


class JsonSink:
    def __init__(self, context: SinkContext) -> None:
        self._dir = context.output_dir

    def publish(self, record: RunRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{record.run_id}.json").write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
