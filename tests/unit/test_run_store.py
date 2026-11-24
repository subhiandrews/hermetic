"""RunStore contract tests. A database-backed store should pass the same suite."""

from __future__ import annotations

import json
import logging
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
from hermetic.history.file_store import _ServiceDir
from tests.factories import make_record, make_result

StoreFactory = Callable[[Path], RunStore]


@pytest.fixture(params=["file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> RunStore:
    factories: dict[str, StoreFactory] = {"file": lambda root: FileRunStore(root / "runs")}
    return factories[request.param](tmp_path)


def test_save_then_get_round_trips(store: RunStore) -> None:
    record = make_record(results=[make_result("passed")])
    store.save(record)
    assert store.get("default", record.run_id) == record


def test_get_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFoundError):
        store.get("default", "nope")


@pytest.mark.parametrize("run_id", ["../secret", "a/b", "index", "", ".hidden"])
def test_get_of_an_id_no_run_can_have_is_not_found(store: RunStore, run_id: str) -> None:
    with pytest.raises(RunNotFoundError):
        store.get("default", run_id)


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
    assert store.get("default", "r1").results[0].status == "passed"


@pytest.mark.parametrize("run_id", ["../x", "a/b", "", ".hidden", "index", "a b"])
def test_run_ids_that_could_name_a_path_are_rejected(store: RunStore, run_id: str) -> None:
    with pytest.raises(RunStoreError):
        store.save(make_record(run_id))
    with pytest.raises(RunStoreError):
        store.get("default", run_id)


def test_runs_are_kept_per_service_and_the_same_id_can_repeat(store: RunStore) -> None:
    alpha = make_record("r1", service="alpha", target="http://alpha")
    beta = make_record("r1", service="beta", target="http://beta")
    store.save(alpha)
    store.save(beta)
    assert store.get("alpha", "r1") == alpha and store.get("beta", "r1") == beta
    assert [s.target for s in store.list("alpha")] == ["http://alpha"]
    assert store.list("gamma") == []


def test_list_without_a_service_covers_every_service_newest_first(store: RunStore) -> None:
    store.save(make_record("a1", service="alpha", started_at="2026-01-01T00:00:00+00:00"))
    store.save(make_record("b1", service="beta", started_at="2026-01-03T00:00:00+00:00"))
    store.save(make_record("a2", service="alpha", started_at="2026-01-02T00:00:00+00:00"))
    assert [(s.service, s.run_id) for s in store.list()] == [
        ("beta", "b1"),
        ("alpha", "a2"),
        ("alpha", "a1"),
    ]


def test_services_lists_each_with_its_run_count_and_latest_run(store: RunStore) -> None:
    assert store.services() == []
    store.save(make_record("a1", service="alpha", started_at="2026-01-01T00:00:00+00:00"))
    store.save(make_record("a2", service="alpha", started_at="2026-01-02T00:00:00+00:00"))
    store.save(make_record("b1", service="beta"))
    found = store.services()
    assert [(s.service, s.runs, s.latest.run_id) for s in found] == [
        ("alpha", 2, "a2"),
        ("beta", 1, "b1"),
    ]


@pytest.mark.parametrize("service", ["../x", "Alpha", "", "a/b"])
def test_a_run_cannot_be_looked_up_under_an_invalid_service(store: RunStore, service: str) -> None:
    store.save(make_record("r1"))
    with pytest.raises(RunNotFoundError):
        store.get(service, "r1")
    assert store.list(service) == []


def test_a_run_of_the_wrong_service_is_not_found(store: RunStore) -> None:
    store.save(make_record("r1", service="alpha"))
    with pytest.raises(RunNotFoundError):
        store.get("beta", "r1")


def test_a_record_cannot_name_an_invalid_service() -> None:
    with pytest.raises(ValueError, match="service"):
        make_record("r1", service="../escape")


def test_a_record_from_before_services_existed_reads_as_the_default_service() -> None:
    legacy = make_record("r1").model_dump(mode="json", exclude={"service", "schema_version"})
    record = RunRecord.model_validate(legacy)
    assert (record.service, record.schema_version) == ("default", 1)


# --- history written before services existed (flat <root>/<run_id>.json) ---


def write_flat(root: Path, run_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    legacy = make_record(run_id).model_dump(mode="json", exclude={"service", "schema_version"})
    (root / f"{run_id}.json").write_text(json.dumps(legacy))


def test_flat_history_moves_under_the_default_service_on_first_use(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    write_flat(root, "old1")
    write_flat(root, "old2")
    (root / "index.json").write_text("[]")  # the old index no longer describes the layout
    store = FileRunStore(root)
    assert sorted(s.run_id for s in store.list("default")) == ["old1", "old2"]
    assert store.get("default", "old1").service == "default"
    assert sorted(p.name for p in root.iterdir()) == ["default"]
    assert [s.service for s in store.services()] == ["default"]


def test_migration_keeps_a_run_the_default_service_already_has(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "runs"
    FileRunStore(root).save(make_record("same", target="http://kept"))
    write_flat(root, "same")
    with caplog.at_level(logging.WARNING, logger="hermetic.history"):
        store = FileRunStore(root)
        assert store.get("default", "same").target == "http://kept"
    assert (root / "same.json").exists()  # left where it was, not overwritten or deleted
    assert "same.json was left where it is" in caplog.text  # and said so


def test_migration_happens_once_per_store_and_a_failure_is_retried(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    write_flat(root, "old1")
    (root / "default").write_text("a file where the service directory should be")
    store = FileRunStore(root)
    with pytest.raises(RunStoreError, match="cannot move the earlier run history"):
        store.list()
    (root / "default").unlink()
    assert [s.run_id for s in store.list()] == ["old1"]  # the failure was not remembered
    write_flat(root, "late")
    assert [s.run_id for s in store.list()] == ["old1"]  # a finished migration is not repeated


def test_migration_falls_back_to_a_rename_where_hard_links_are_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    write_flat(root, "old1")

    def no_links(src: object, dst: object) -> None:
        raise PermissionError("hard links are not supported here")

    monkeypatch.setattr("hermetic.history.file_store.os.link", no_links)
    assert [s.run_id for s in FileRunStore(root).list()] == ["old1"]
    assert not (root / "old1.json").exists()


def test_the_rename_fallback_tolerates_a_file_another_process_moved_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    write_flat(root, "old1")

    def no_links(src: object, dst: object) -> None:
        raise PermissionError("hard links are not supported here")

    def gone(src: object, dst: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("hermetic.history.file_store.os.link", no_links)
    monkeypatch.setattr("hermetic.history.file_store.os.replace", gone)
    assert FileRunStore(root).list() == []  # no error: the other process has the run


def test_the_rename_fallback_does_not_overwrite_a_run_the_default_service_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "runs"
    FileRunStore(root).save(make_record("same", target="http://kept"))
    write_flat(root, "same")

    def no_links(src: object, dst: object) -> None:
        raise PermissionError("hard links are not supported here")

    monkeypatch.setattr("hermetic.history.file_store.os.link", no_links)
    with caplog.at_level(logging.WARNING, logger="hermetic.history"):
        store = FileRunStore(root)
        assert store.get("default", "same").target == "http://kept"
    assert (root / "same.json").exists() and "same.json was left where it is" in caplog.text


def test_a_file_another_process_already_moved_is_skipped_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    write_flat(root, "old1")
    write_flat(root, "old2")

    def vanished(src: object, dst: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("hermetic.history.file_store.os.link", vanished)
    assert FileRunStore(root).list() == []  # nothing was readable, and nothing raised


def test_migration_ignores_files_that_are_not_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (root / ".hidden.json").write_text("{}")
    with caplog.at_level(logging.WARNING, logger="hermetic.history"):
        store = FileRunStore(root)
        assert store.list() == []
    assert (root / ".hidden.json").exists() and ".hidden.json was left" in caplog.text


def test_the_history_directory_may_not_exist_yet(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "nope")
    assert store.list() == [] and store.services() == []


def test_a_service_directory_without_runs_is_not_listed(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "empty").mkdir(parents=True)
    (root / "Not A Service").mkdir()
    store = FileRunStore(root)
    store.save(make_record("r1", service="alpha"))
    assert [s.service for s in store.services()] == ["alpha"]


# --- FileRunStore specifics: layout, index rebuild, corrupt files, I/O failure ---


def test_file_layout_is_one_json_per_run_plus_index(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    FileRunStore(root).save(make_record("r1"))
    assert [p.name for p in root.iterdir()] == ["default"]
    assert sorted(p.name for p in (root / "default").iterdir()) == ["index.json", "r1.json"]
    assert json.loads((root / "default" / "index.json").read_text())[0]["run_id"] == "r1"


@pytest.mark.parametrize("damage", [None, "not json", '[{"run_id": 1}]'])
def test_missing_or_corrupt_index_is_rebuilt_from_run_files(
    tmp_path: Path, damage: str | None
) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    if damage is None:
        (root / "default" / "index.json").unlink()
    else:
        (root / "default" / "index.json").write_text(damage)
    assert [s.run_id for s in store.list()] == ["r1"]


def test_rebuild_skips_corrupt_and_oddly_named_files(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("good"))
    (root / "default" / "index.json").unlink()
    (root / "default" / "bad.json").write_text("{")
    (root / "default" / ".odd.json").write_text("{}")
    assert [s.run_id for s in store.list()] == ["good"]


def test_get_corrupt_run_raises_store_error(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "default").mkdir(parents=True)
    (root / "default" / "bad.json").write_text('{"run_id": "bad"}')
    with pytest.raises(RunStoreError, match="corrupt"):
        FileRunStore(root).get("default", "bad")


def test_get_undecodable_run_raises_store_error(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (root / "bin.json").write_bytes(b"\xff\xfe")
    with pytest.raises(RunStoreError, match="cannot read"):
        FileRunStore(root).get("default", "bin")


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
    assert list((root / "default").iterdir()) == []


def test_a_failed_index_write_does_not_hide_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(make_record("r1"))

    def boom(self: _ServiceDir, summaries: object) -> None:
        raise OSError("index write failed")

    with monkeypatch.context() as patch:
        patch.setattr(_ServiceDir, "_write_index", boom)
        with pytest.raises(RunStoreError):
            store.save(make_record("r2"))
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2"]


def test_an_interrupt_between_the_two_writes_does_not_hide_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(make_record("r1"))

    def interrupted(self: _ServiceDir, summaries: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(_ServiceDir, "_write_index", interrupted)
        with pytest.raises(KeyboardInterrupt):
            store.save(make_record("r2"))
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2"]


def test_a_rebuilt_index_is_written_back(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "default" / "index.json").unlink()
    assert [s.run_id for s in store.list()] == ["r1"]
    assert json.loads((root / "default" / "index.json").read_text())[0]["run_id"] == "r1"


def test_a_rebuild_that_cannot_be_saved_still_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "default" / "index.json").write_text("not json")

    def refuse(self: _ServiceDir, summaries: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(_ServiceDir, "_write_index", refuse)  # independent of the user's rights
    assert [s.run_id for s in store.list()] == ["r1"]


def test_a_valid_index_that_lacks_a_run_is_repaired(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    stale = (root / "default" / "index.json").read_text()
    store.save(make_record("r2"))
    (root / "default" / "index.json").write_text(stale)  # the last writer of a race, without r2
    (root / "default" / "broken.json").write_text("{")  # a corrupt run file is skipped
    assert sorted(s.run_id for s in store.list()) == ["r1", "r2"]
    assert "r2" in (root / "default" / "index.json").read_text()


def test_a_save_after_a_lost_update_keeps_every_run(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    stale = (root / "default" / "index.json").read_text()
    store.save(make_record("r2"))
    (root / "default" / "index.json").write_text(stale)
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
    (root / "default" / "broken.json").write_text("{")
    before = (root / "default" / "index.json").stat().st_mtime_ns
    assert [s.run_id for s in store.list()] == ["r1"]
    assert [s.run_id for s in store.list()] == ["r1"]
    assert (root / "default" / "index.json").stat().st_mtime_ns == before


def test_an_unreadable_run_file_is_parsed_once_until_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    store.save(make_record("r1"))
    (root / "default" / "broken.json").write_text("{")
    reads: list[str] = []
    original = _ServiceDir.get

    def counting_get(self: _ServiceDir, run_id: str) -> RunRecord:
        reads.append(run_id)
        return original(self, run_id)

    monkeypatch.setattr(_ServiceDir, "get", counting_get)
    store.list()
    store.list()
    assert reads == ["broken"]  # not parsed again on the second read
    broken = root / "default" / "broken.json"
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
    monkeypatch.setattr(_ServiceDir, "_run_ids", lambda self: ["r1", "ghost"])
    assert [s.run_id for s in store.list()] == ["r1"]
