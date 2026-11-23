"""Report sinks: where a finished run goes. Third parties add sinks via entry points."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

from hermetic.history.models import RunRecord

ENTRY_POINT_GROUP = "hermetic.sinks"


class SinkError(Exception):
    """A sink is unknown, failed to load, or failed to publish.

    When publishing failed after the run happened, `record` holds the saved run so the
    caller can still decide the exit code from its results.
    """

    def __init__(self, message: str, record: RunRecord | None = None) -> None:
        super().__init__(message)
        self.record = record


class ReportSink(Protocol):
    def publish(self, record: RunRecord) -> None:
        """Deliver the run somewhere (a file, a terminal, an issue tracker)."""
        ...


@dataclass(frozen=True)
class SinkContext:
    """What a sink factory is given. Grows only by adding fields."""

    output_dir: Path


SinkFactory = Callable[[SinkContext], ReportSink]


def load_sinks(names: list[str], context: SinkContext) -> list[ReportSink]:
    """Build the named sinks. Built-ins win; other names resolve through entry points.

    An entry point in the `hermetic.sinks` group must name a SinkFactory.
    """
    from hermetic.report import BUILTIN_SINKS  # deferred: the package imports this module

    third_party = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
    sinks: list[ReportSink] = []
    for name in names:
        if name in BUILTIN_SINKS:
            factory: SinkFactory = BUILTIN_SINKS[name]
        elif name in third_party:
            try:
                factory = third_party[name].load()
            except Exception as exc:  # a plugin can fail in any way at import time
                raise SinkError(f"sink {name!r} failed to load: {exc}") from exc
        else:
            known = ", ".join(sorted({*BUILTIN_SINKS, *third_party}))
            raise SinkError(f"unknown sink {name!r} (available: {known})")
        try:
            sinks.append(factory(context))
        except Exception as exc:  # a third-party factory can fail in any way
            raise SinkError(f"sink {name!r} could not be created: {exc}") from exc
    return sinks
