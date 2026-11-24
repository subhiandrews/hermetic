"""Flat-file RunStore: `<root>/<service>/<run_id>.json` per run, plus an `index.json` each."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from hermetic.history.models import RunRecord, RunSummary, ServiceSummary, summarize
from hermetic.history.store import RunNotFoundError, RunStoreError
from hermetic.service import DEFAULT_SERVICE, is_valid_service

_logger = logging.getLogger("hermetic.history")
INDEX_NAME = "index.json"
# run_id becomes a file name, so it must not be able to name a path.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
RunIds = list[str]  # aliases: `list` is shadowed by the method inside the class
Summaries = list[RunSummary]
Services = list[ServiceSummary]
Names = list[str]
_INDEX = TypeAdapter(Summaries)


def _valid_run_id(run_id: str) -> bool:
    return _RUN_ID.fullmatch(run_id) is not None and run_id.lower() != INDEX_NAME.removesuffix(
        ".json"
    )


def _check_run_id(run_id: str) -> None:
    if not _valid_run_id(run_id):
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


class _ServiceDir:
    """The runs of one service: the directory `<root>/<service>`."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # run id -> mtime of a run file that failed to parse, so a long-lived store (the web UI)
        # does not re-parse the same corrupt file on every read. A changed file is tried again.
        self._unreadable: dict[str, int] = {}

    def save(self, record: RunRecord) -> None:
        _check_run_id(record.run_id)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            summaries = [s for s in self._read_index() if s.run_id != record.run_id]
            _write_atomic(self._root / f"{record.run_id}.json", record.model_dump_json(indent=2))
            summaries.append(summarize(record))
            self._write_index(summaries)
        except OSError as exc:
            self._drop_index()
            raise RunStoreError(f"cannot write run {record.run_id}: {exc}") from exc
        except BaseException:  # an interrupt between the two writes
            self._drop_index()
            raise

    def _drop_index(self) -> None:
        """The run file may exist while the index lacks it; remove the index so the next read
        rebuilds it from the run files instead of permanently omitting the run."""
        with contextlib.suppress(OSError):
            (self._root / INDEX_NAME).unlink(missing_ok=True)

    def get(self, run_id: str) -> RunRecord:
        if not _valid_run_id(run_id):  # no run can have this id, so it is simply not found
            raise RunNotFoundError(f"no run {run_id!r}")
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
        """The index, reconciled with the run files.

        A missing or unreadable index is rebuilt from every run file. A valid index that lacks
        a run (two processes saved at once and the last writer won, or a crash between the
        two writes) gets that run added. Either way the result is written back, so each run
        file is parsed once. A run file that cannot be parsed is remembered by modification
        time, so it is not parsed again until it changes.
        """
        try:
            summaries = _INDEX.validate_json((self._root / INDEX_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError):
            summaries = self._rebuild_index()
        else:
            known = {summary.run_id for summary in summaries}
            missing = [name for name in self._run_ids() if name not in known]
            if not missing:
                return summaries
            added = self._summaries_of(missing)
            if not added:  # only unreadable run files are missing: nothing new to write
                return summaries
            summaries = [*summaries, *added]
        with contextlib.suppress(OSError):
            self._write_index(summaries)
        return summaries

    def _run_ids(self) -> RunIds:
        return sorted(p.stem for p in self._root.glob("*.json") if p.name != INDEX_NAME)

    def _summaries_of(self, run_ids: RunIds) -> Summaries:
        summaries: Summaries = []
        for run_id in run_ids:
            mtime = self._mtime(run_id)
            if self._unreadable.get(run_id, -1) == mtime:
                continue
            try:
                summaries.append(summarize(self.get(run_id)))
            except RunStoreError:  # a corrupt run file is skipped, not fatal
                self._unreadable[run_id] = mtime
        return summaries

    def _mtime(self, run_id: str) -> int:
        try:
            return (self._root / f"{run_id}.json").stat().st_mtime_ns
        except OSError:  # vanished since the directory was listed
            return 0

    def _rebuild_index(self) -> Summaries:
        return self._summaries_of(self._run_ids())

    def _write_index(self, summaries: Summaries) -> None:
        _write_atomic(self._root / INDEX_NAME, json.dumps(_INDEX.dump_python(summaries), indent=2))


class FileRunStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._dirs: dict[str, _ServiceDir] = {}  # keeps each directory's unreadable-file memory
        self._migrated = False

    def save(self, record: RunRecord) -> None:
        self._migrate_flat_history()
        self._dir(record.service).save(record)

    def get(self, service: str, run_id: str) -> RunRecord:
        if not is_valid_service(service):  # no run can be filed under it, so it is not found
            raise RunNotFoundError(f"no run {run_id!r} for service {service!r}")
        self._migrate_flat_history()
        return self._dir(service).get(run_id)

    def list(self, service: str | None = None) -> Summaries:
        self._migrate_flat_history()
        if service is not None:
            return self._dir(service).list() if is_valid_service(service) else []
        return _newest_first([s for name in self._service_names() for s in self._dir(name).list()])

    def services(self) -> Services:
        self._migrate_flat_history()
        found: Services = []
        for name in self._service_names():
            summaries = self._dir(name).list()
            if summaries:
                found.append(ServiceSummary(service=name, runs=len(summaries), latest=summaries[0]))
        return found

    def _dir(self, service: str) -> _ServiceDir:
        if service not in self._dirs:
            self._dirs[service] = _ServiceDir(self._root / service)
        return self._dirs[service]

    def _service_names(self) -> Names:
        try:
            entries = list(self._root.iterdir())
        except OSError:  # no history yet
            return []
        return sorted(p.name for p in entries if p.is_dir() and is_valid_service(p.name))

    def _migrate_flat_history(self) -> None:
        """Once per store, move run files from before services existed (`<root>/<run_id>.json`)
        under the default service. Only a file that parses as a run record is moved, and only an
        `index.json` that parses as the old index is dropped; anything else in the directory is
        not ours and stays. Nothing is overwritten. Where the move is not possible (a read-only
        directory) it logs once and carries on, and the next process tries again."""
        if self._migrated:
            return
        self._migrated = True
        try:
            earlier = [p for p in sorted(self._root.glob("*.json")) if _is_earlier_history(p)]
            if earlier:
                default_dir = self._root / DEFAULT_SERVICE
                default_dir.mkdir(exist_ok=True)
                for path in earlier:
                    _move_earlier_history(path, default_dir)
        except OSError as exc:
            _logger.warning("earlier run history could not be moved under 'default': %s", exc)


def _is_earlier_history(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        if path.name == INDEX_NAME:
            _INDEX.validate_json(text)
        else:
            RunRecord.model_validate_json(text)
            return _valid_run_id(path.stem)
    except (OSError, UnicodeDecodeError, ValidationError):
        return False
    return True


def _move_earlier_history(path: Path, default_dir: Path) -> None:
    if path.name == INDEX_NAME:  # each service's index is rebuilt from its run files
        path.unlink(missing_ok=True)
        return
    destination = default_dir / path.name
    try:
        os.link(path, destination)  # fails, rather than overwrites, if it exists
    except (FileExistsError, FileNotFoundError):  # the default service has it, or it already moved
        return
    except OSError:
        # A filesystem without hard links: rename, unless the run is already there. Unlike the
        # link, this check and the rename are two steps, so it is best effort against a process
        # writing the same name at that instant.
        if not destination.exists():
            with contextlib.suppress(FileNotFoundError):  # moved by another process
                os.replace(path, destination)
        return
    path.unlink(missing_ok=True)


def _newest_first(summaries: Summaries) -> Summaries:
    return sorted(summaries, key=lambda s: (s.started_at, s.run_id), reverse=True)
