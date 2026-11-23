"""RunStore contract tests. A database-backed store should pass the same suite."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from hermetic.history import (
    FileRunStore,
    RunNotFoundError,
    RunRecord,
    RunStore,
    RunStoreError,
    SuiteChange,
    summarize,
)
from tests.factories import make_record, make_result

StoreFactory = Callable[[Path], RunStore]


@pytest.fixture(params=["file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> RunStore:
    factories: dict[str, StoreFactory] = {"file": lambda root: FileRunStore(root / "runs")}
    return factories[request.param](tmp_path)


def test_save_then_get_round_trips(store: RunStore) -> None:
    record = make_record(results=[make_result("passed")])
    store.save(record)
    assert store.get(record.run_id) == record


def test_get_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFoundError):
        store.get("nope")


def test_list_is_empty_for_a_new_store(store: RunStore) -> None:
    assert store.list() == []


def test_list_is_newest_first_with_counts(store: RunStore) -> None:
    older = make_record("old", started_at="2026-01-01T00:00:00+00:00")
    newer = make_record(
        "new",
        started_at="2026-01-02T00:00:00+00:00",
        results=[make_result("passed"), make_result("failed"), make_result("error")],
        changes=[
            SuiteChange(kind="added", test_id="a", reason="r"),
            SuiteChange(kind="modified", test_id="m", reason="r"),
            SuiteChange(kind="dropped", test_id="d", reason="r"),
        ],
    )
    store.save(older)
    store.save(newer)
    summaries = store.list()
    assert [s.run_id for s in summaries] == ["new", "old"]
    assert summaries[0] == summarize(newer)
    assert (summaries[0].passed, summaries[0].failed, summaries[0].errors) == (1, 1, 1)
    assert (summaries[0].added, summaries[0].modified, summaries[0].dropped) == (1, 1, 1)


def test_saving_the_same_run_id_replaces_it(store: RunStore) -> None:
    store.save(make_record("r1", results=[make_result("failed")]))
    store.save(make_record("r1", results=[make_result("passed")]))
    assert [s.passed for s in store.list()] == [1]
    assert store.get("r1").results[0].status == "passed"


@pytest.mark.parametrize("run_id", ["../x", "a/b", "", ".hidden", "index", "a b"])
def test_run_ids_that_could_name_a_path_are_rejected(store: RunStore, run_id: str) -> None:
    with pytest.raises(RunStoreError):
        store.save(make_record(run_id))
    with pytest.raises(RunStoreError):
        store.get(run_id)


# --- FileRunStore specifics: layout, index rebuild, corrupt files, I/O failure ---


def test_file_layout_is_one_json_per_run_plus_index(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    FileRunStore(root).save(make_record("r1"))
    assert sorted(p.name for p in root.iterdir()) == ["index.json", "r1.json"]
    assert json.loads((root / "index.json").read_text())[0]["run_id"] == "r1"


@pytest.mark.parametrize("damage", [None, "not json", '[{"run_id": 1}]'])
def test_missing_or_corrupt_index_is_rebuilt_from_run_files(
    tmp_path: Path, damage: str | None
) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    if damage is None:
        (root / "index.json").unlink()
    else:
        (root / "index.json").write_text(damage)
    assert [s.run_id for s in store.list()] == ["r1"]


def test_rebuild_skips_corrupt_and_oddly_named_files(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("good"))
    (root / "index.json").unlink()
    (root / "bad.json").write_text("{")
    (root / ".odd.json").write_text("{}")
    assert [s.run_id for s in store.list()] == ["good"]


def test_get_corrupt_run_raises_store_error(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (root / "bad.json").write_text('{"run_id": "bad"}')
    with pytest.raises(RunStoreError, match="corrupt"):
        FileRunStore(root).get("bad")


def test_get_undecodable_run_raises_store_error(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (root / "bin.json").write_bytes(b"\xff\xfe")
    with pytest.raises(RunStoreError, match="cannot read"):
        FileRunStore(root).get("bin")


def test_save_wraps_io_errors(tmp_path: Path) -> None:
    blocker = tmp_path / "runs"
    blocker.write_text("a file where the directory should be")
    with pytest.raises(RunStoreError, match="cannot write"):
        FileRunStore(blocker).save(make_record("r1"))


def test_failed_write_leaves_no_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hermetic.history.file_store.os.replace", boom)
    with pytest.raises(RunStoreError):
        store.save(make_record("r1"))
    assert list(root.iterdir()) == []


def test_a_failed_index_write_does_not_hide_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(make_record("r1"))

    def boom(self: FileRunStore, summaries: object) -> None:
        raise OSError("index write failed")

    with monkeypatch.context() as patch:
        patch.setattr(FileRunStore, "_write_index", boom)
        with pytest.raises(RunStoreError):
            store.save(make_record("r2"))
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2"]


def test_an_interrupt_between_the_two_writes_does_not_hide_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(make_record("r1"))

    def interrupted(self: FileRunStore, summaries: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(FileRunStore, "_write_index", interrupted)
        with pytest.raises(KeyboardInterrupt):
            store.save(make_record("r2"))
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2"]


def test_a_rebuilt_index_is_written_back(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "index.json").unlink()
    assert [s.run_id for s in store.list()] == ["r1"]
    assert json.loads((root / "index.json").read_text())[0]["run_id"] == "r1"


def test_a_rebuild_that_cannot_be_saved_still_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "index.json").write_text("not json")

    def refuse(self: FileRunStore, summaries: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(FileRunStore, "_write_index", refuse)  # independent of the user's rights
    assert [s.run_id for s in store.list()] == ["r1"]


def test_a_valid_index_that_lacks_a_run_is_repaired(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    stale = (root / "index.json").read_text()
    store.save(make_record("r2"))
    (root / "index.json").write_text(stale)  # the last writer of a race, without r2
    (root / "broken.json").write_text("{")  # a corrupt run file is skipped
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2"]
    assert "r2" in (root / "index.json").read_text()


def test_a_save_after_a_lost_update_keeps_every_run(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    stale = (root / "index.json").read_text()
    store.save(make_record("r2"))
    (root / "index.json").write_text(stale)
    store.save(make_record("r3"))
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2", "r3"]


@pytest.mark.parametrize("run_id", ["Index", "INDEX"])
def test_the_reserved_index_name_is_rejected_in_any_case(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(RunStoreError):
        FileRunStore(tmp_path / "runs").save(make_record(run_id))


def test_an_unreadable_run_file_does_not_rewrite_the_index_on_every_read(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "broken.json").write_text("{")
    before = (root / "index.json").stat().st_mtime_ns
    assert [s.run_id for s in store.list()] == ["r1"]
    assert [s.run_id for s in store.list()] == ["r1"]
    assert (root / "index.json").stat().st_mtime_ns == before


def test_an_unreadable_run_file_is_parsed_once_until_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "broken.json").write_text("{")
    reads: list[str] = []
    original = FileRunStore.get

    def counting_get(self: FileRunStore, run_id: str) -> RunRecord:
        reads.append(run_id)
        return original(self, run_id)

    monkeypatch.setattr(FileRunStore, "get", counting_get)
    store.list()
    store.list()
    assert reads == ["broken"]  # not parsed again on the second read
    broken = root / "broken.json"
    stamp = broken.stat().st_mtime_ns
    broken.write_text(make_record("broken").model_dump_json())
    os.utime(broken, ns=(stamp + 10**9, stamp + 10**9))  # coarse clocks can repeat a timestamp
    assert sorted(s.run_id for s in store.list()) == ["broken", "r1"]
    assert reads == ["broken", "broken"]  # a changed file is tried again


def test_a_run_file_that_vanishes_mid_scan_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(make_record("r1"))
    # the directory listing names a file that is gone by the time it is read
    monkeypatch.setattr(FileRunStore, "_run_ids", lambda self: ["r1", "ghost"])
    assert [s.run_id for s in store.list()] == ["r1"]
