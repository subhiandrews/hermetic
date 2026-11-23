"""Flat-file RunStore: `<root>/<run_id>.json` per run plus `<root>/index.json`."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from hermetic.history.models import RunRecord, RunSummary, summarize
from hermetic.history.store import RunNotFoundError, RunStoreError

INDEX_NAME = "index.json"
# run_id becomes a file name, so it must not be able to name a path.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
Summaries = list[RunSummary]  # alias: `list` is shadowed by the method inside the class
_INDEX = TypeAdapter(Summaries)


def _check_run_id(run_id: str) -> None:
    if _RUN_ID.fullmatch(run_id) is None or run_id == INDEX_NAME.removesuffix(".json"):
        raise RunStoreError(f"invalid run id: {run_id!r}")


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory so a crash never leaves a torn file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class FileRunStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, record: RunRecord) -> None:
        _check_run_id(record.run_id)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            summaries = [s for s in self._read_index() if s.run_id != record.run_id]
            _write_atomic(self._root / f"{record.run_id}.json", record.model_dump_json(indent=2))
            summaries.append(summarize(record))
            self._write_index(summaries)
        except OSError as exc:
            # The run file may exist while the index lacks it; drop the index so the next
            # read rebuilds it from the run files instead of permanently omitting the run.
            with contextlib.suppress(OSError):
                (self._root / INDEX_NAME).unlink(missing_ok=True)
            raise RunStoreError(f"cannot write run {record.run_id}: {exc}") from exc

    def get(self, run_id: str) -> RunRecord:
        _check_run_id(run_id)
        path = self._root / f"{run_id}.json"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RunNotFoundError(f"no run {run_id}") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise RunStoreError(f"cannot read run {run_id}: {exc}") from exc
        try:
            return RunRecord.model_validate_json(text)
        except ValidationError as exc:
            raise RunStoreError(f"run {run_id} is corrupt: {exc}") from exc

    def list(self) -> Summaries:
        return _newest_first(self._read_index())

    def _read_index(self) -> Summaries:
        """The index, rebuilt from the run files when it is missing or unreadable."""
        try:
            return _INDEX.validate_json((self._root / INDEX_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError):
            return self._rebuild_index()

    def _rebuild_index(self) -> Summaries:
        summaries: Summaries = []
        for path in sorted(self._root.glob("*.json")):
            if path.name == INDEX_NAME:
                continue
            with contextlib.suppress(RunStoreError):  # a corrupt run file is skipped, not fatal
                summaries.append(summarize(self.get(path.stem)))
        return summaries

    def _write_index(self, summaries: Summaries) -> None:
        _write_atomic(self._root / INDEX_NAME, json.dumps(_INDEX.dump_python(summaries), indent=2))


def _newest_first(summaries: Summaries) -> Summaries:
    return sorted(summaries, key=lambda s: (s.started_at, s.run_id), reverse=True)
