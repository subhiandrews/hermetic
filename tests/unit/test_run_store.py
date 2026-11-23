"""RunStore contract tests. A database-backed store should pass the same suite."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from hermetic.history import (
    FileRunStore,
    RunNotFoundError,
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
