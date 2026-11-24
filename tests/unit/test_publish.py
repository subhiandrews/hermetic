from __future__ import annotations

import os
import zipfile
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.diffing.manifest import MAX_MANIFEST_BYTES
from hermetic.generate.validate import MAX_SOURCE_BYTES
from hermetic.publish import (
    BUILTIN_PUBLISHERS,
    ExportError,
    LocalPublisher,
    PublisherError,
    load_publisher,
    write_suite_zip,
)
from hermetic.publish.archive import ZIP_EPOCH, suite_files

runner = CliRunner()

MANIFEST = Path(".hermetic") / "manifest.json"


def make_suite(root: Path, *modules: str, manifest: str | None = '{"version": 1}') -> Path:
    """A suite directory with the given test modules, a manifest, and files that are not tests."""
    root.mkdir(parents=True, exist_ok=True)
    for name in modules:
        (root / name).write_text(f"# {name}\n")
    if manifest is not None:
        (root / MANIFEST).parent.mkdir(exist_ok=True)
        (root / MANIFEST).write_text(manifest)
    (root / "notes.txt").write_text("not generated")
    (root / "conftest.py").write_text("# not a test module\n")
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "test_a.cpython-312.pyc").write_bytes(b"\0")
    return root


# --- the archive ---


def test_a_suite_packs_its_tests_and_manifest_under_the_service_name(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_b.py", "test_a.py")
    packed = write_suite_zip("petstore", suite, tmp_path / "out.zip")
    assert packed == [
        "petstore/.hermetic/manifest.json",
        "petstore/test_a.py",
        "petstore/test_b.py",
    ]
    with zipfile.ZipFile(tmp_path / "out.zip") as archive:
        assert archive.namelist() == packed
        assert archive.read("petstore/test_a.py") == b"# test_a.py\n"
        assert archive.read("petstore/.hermetic/manifest.json") == b'{"version": 1}'


def test_the_archive_unpacks_into_a_directory_of_its_own(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    write_suite_zip("svc", suite, tmp_path / "out.zip")
    with zipfile.ZipFile(tmp_path / "out.zip") as archive:
        archive.extractall(tmp_path / "unpacked")
    assert sorted(p.name for p in (tmp_path / "unpacked").iterdir()) == ["svc"]
    assert (tmp_path / "unpacked" / "svc" / "test_a.py").read_text() == "# test_a.py\n"


def test_the_same_suite_packs_to_the_same_bytes_whatever_its_files_say(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py", "test_b.py")
    write_suite_zip("svc", suite, tmp_path / "first.zip")
    for path in (suite / "test_a.py", suite / "test_b.py", suite / MANIFEST):
        os.utime(path, (1_000_000_000, 1_000_000_000))
        path.chmod(0o600)
    write_suite_zip("svc", suite, tmp_path / "second.zip")
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    with zipfile.ZipFile(tmp_path / "first.zip") as archive:
        for info in archive.infolist():
            assert info.date_time == ZIP_EPOCH
            assert info.external_attr >> 16 == 0o100644


def test_only_generated_files_are_packed(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    (suite / "test_dir.py").mkdir()  # a directory that happens to look like a module
    assert suite_files(suite) == [
        (".hermetic/manifest.json", MAX_MANIFEST_BYTES),
        ("test_a.py", MAX_SOURCE_BYTES),
    ]


def test_a_symlink_is_never_packed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    suite = make_suite(tmp_path / "suite", "test_a.py")
    (suite / "test_link.py").symlink_to(outside)
    assert [name for name, _ in suite_files(suite)] == [".hermetic/manifest.json", "test_a.py"]


def test_a_manifest_that_is_a_symlink_or_in_a_symlinked_directory_is_not_packed(
    tmp_path: Path,
) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py", manifest=None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "manifest.json").write_text("{}")
    (suite / ".hermetic").symlink_to(elsewhere)  # the directory is a link
    assert [name for name, _ in suite_files(suite)] == ["test_a.py"]
    (suite / ".hermetic").unlink()
    (suite / ".hermetic").mkdir()
    (suite / MANIFEST).symlink_to(elsewhere / "manifest.json")  # the file is a link
    assert [name for name, _ in suite_files(suite)] == ["test_a.py"]


def test_a_suite_without_a_manifest_still_packs_its_tests(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py", manifest=None)
    assert write_suite_zip("svc", suite, tmp_path / "out.zip") == ["svc/test_a.py"]


@pytest.mark.parametrize("modules", [(), ("helper.py",)])
def test_a_suite_with_no_generated_tests_is_refused(
    tmp_path: Path, modules: tuple[str, ...]
) -> None:
    suite = make_suite(tmp_path / "suite", *modules)  # only a manifest and other files
    with pytest.raises(ExportError, match="no generated tests"):
        write_suite_zip("svc", suite, tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()


def test_a_missing_suite_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="no generated tests"):
        write_suite_zip("svc", tmp_path / "nope", tmp_path / "out.zip")


def test_a_test_larger_than_any_generated_one_is_refused(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    (suite / "test_a.py").write_bytes(b"#" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(ExportError, match=r"test_a\.py is larger"):
        write_suite_zip("svc", suite, tmp_path / "out.zip")


def test_a_file_that_becomes_a_symlink_after_listing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    real = suite_files(suite)
    (suite / "test_a.py").unlink()
    (suite / "test_a.py").symlink_to(outside)
    monkeypatch.setattr("hermetic.publish.archive.suite_files", lambda _: real)
    with pytest.raises(OSError):
        write_suite_zip("svc", suite, tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()


def test_a_file_that_is_not_regular_after_listing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    real = suite_files(suite)
    (suite / "test_a.py").unlink()
    (suite / "test_a.py").mkdir()
    monkeypatch.setattr("hermetic.publish.archive.suite_files", lambda _: real)
    with pytest.raises(ExportError, match="not a regular file"):
        write_suite_zip("svc", suite, tmp_path / "out.zip")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_file_swapped_for_a_fifo_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    real = suite_files(suite)
    (suite / "test_a.py").unlink()
    os.mkfifo(suite / "test_a.py")
    monkeypatch.setattr("hermetic.publish.archive.suite_files", lambda _: real)
    with pytest.raises(ExportError, match="not a regular file"):
        write_suite_zip("svc", suite, tmp_path / "out.zip")


def test_a_manifest_larger_than_the_generator_reads_is_refused(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    (suite / MANIFEST).write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ExportError, match=r"manifest\.json is larger"):
        write_suite_zip("svc", suite, tmp_path / "out.zip")


def test_an_existing_archive_is_replaced_whole(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    destination = tmp_path / "out.zip"
    destination.write_bytes(b"an older archive")
    write_suite_zip("svc", suite, destination)
    assert zipfile.is_zipfile(destination)
    assert destination.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".export-")] == []


def test_a_failure_while_writing_leaves_the_old_archive_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    destination = tmp_path / "out.zip"
    destination.write_bytes(b"an older archive")

    def refuse(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hermetic.publish.archive.os.replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        write_suite_zip("svc", suite, destination)
    assert destination.read_bytes() == b"an older archive"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".export-")] == []


# --- publishers ---


class RecordingPublisher:
    published: list[tuple[str, Path]] = []  # noqa: RUF012

    def publish(self, service: str, suite_dir: Path) -> str:
        RecordingPublisher.published.append((service, suite_dir))
        return f"recorded:{service}"


def _entry_points(*eps: EntryPoint) -> object:
    return lambda group: [ep for ep in eps if ep.group == group]


def test_the_builtin_publisher_is_local() -> None:
    assert set(BUILTIN_PUBLISHERS) == {"local"}


def test_the_local_publisher_leaves_the_suite_where_it_is(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    before = sorted(p.name for p in suite.iterdir())
    where = load_publisher("local").publish("svc", suite)
    assert where == str(suite)
    assert sorted(p.name for p in suite.iterdir()) == before
    assert isinstance(load_publisher("local"), LocalPublisher)


def test_third_party_publishers_load_through_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = EntryPoint("custom", f"{__name__}:RecordingPublisher", "hermetic.publishers")
    monkeypatch.setattr("hermetic.publish.publishers.entry_points", _entry_points(ep))
    monkeypatch.setattr(RecordingPublisher, "published", [])
    publisher = load_publisher("custom")
    assert publisher.publish("svc", tmp_path) == "recorded:svc"
    assert RecordingPublisher.published == [("svc", tmp_path)]


def test_a_third_party_publisher_cannot_replace_a_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ep = EntryPoint("local", f"{__name__}:RecordingPublisher", "hermetic.publishers")
    monkeypatch.setattr("hermetic.publish.publishers.entry_points", _entry_points(ep))
    assert type(load_publisher("local")) is LocalPublisher


def test_a_publisher_that_fails_to_import_is_a_publisher_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ep = EntryPoint("broken", "no_such_module_xyz:Publisher", "hermetic.publishers")
    monkeypatch.setattr("hermetic.publish.publishers.entry_points", _entry_points(ep))
    with pytest.raises(PublisherError, match="failed to load"):
        load_publisher("broken")


def test_an_unknown_publisher_lists_the_available_ones() -> None:
    with pytest.raises(PublisherError, match=r"unknown publisher 'nope' \(available: local"):
        load_publisher("nope")


def test_a_factory_that_raises_is_a_publisher_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> RecordingPublisher:
        raise RuntimeError("bad config")

    monkeypatch.setitem(BUILTIN_PUBLISHERS, "broken", broken)
    with pytest.raises(PublisherError, match="could not be created: bad config"):
        load_publisher("broken")


# --- the commands ---


def test_export_packs_the_default_suite_of_a_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    make_suite(tmp_path / "tests" / "petstore", "test_a.py")
    result = runner.invoke(app, ["export", "--service", "petstore"])
    assert result.exit_code == 0, result.output
    assert "wrote hermetic-tests-petstore.zip (2 files)" in result.output
    with zipfile.ZipFile(tmp_path / "hermetic-tests-petstore.zip") as archive:
        assert "petstore/test_a.py" in archive.namelist()


def test_export_takes_a_suite_directory_and_an_output_path(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "elsewhere", "test_a.py")
    out = tmp_path / "bundle.zip"
    arguments = ["--service", "svc", "--suite", str(suite), "--output", str(out)]
    result = runner.invoke(app, ["export", *arguments, "--format", "zip"])
    assert result.exit_code == 0, result.output
    assert out.is_file()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--service", "Bad Name"], "invalid service"),
        (["--service", "svc", "--suite", "SUITE_EMPTY"], "no generated tests"),
        (["--service", "svc", "--suite", "SUITE", "--output", "NO_DIR/out.zip"], "error:"),
    ],
)
def test_export_reports_failures_as_exit_2(tmp_path: Path, extra: list[str], message: str) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    empty = tmp_path / "empty"
    empty.mkdir()
    args = [
        a.replace("SUITE_EMPTY", str(empty))
        .replace("SUITE", str(suite))
        .replace("NO_DIR", str(tmp_path / "missing-dir"))
        for a in extra
    ]
    result = runner.invoke(app, ["export", *args])
    assert result.exit_code == 2 and message in result.output


def test_export_refuses_a_format_it_does_not_know(tmp_path: Path) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    arguments = ["--service", "svc", "--suite", str(suite), "--format", "tar"]
    assert runner.invoke(app, ["export", *arguments]).exit_code == 2


def test_publish_with_the_local_publisher_says_where_the_suite_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    make_suite(tmp_path / "tests" / "petstore", "test_a.py")
    result = runner.invoke(app, ["publish", "--service", "petstore"])
    assert result.exit_code == 0, result.output
    assert "published petstore to " + str(Path("tests") / "petstore") in result.output


def test_publish_uses_the_publisher_it_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(BUILTIN_PUBLISHERS, "recording", RecordingPublisher)
    suite = make_suite(tmp_path / "suite", "test_a.py")
    arguments = ["--service", "svc", "--suite", str(suite), "--publisher", "recording"]
    result = runner.invoke(app, ["publish", *arguments])
    assert result.exit_code == 0 and "published svc to recorded:svc" in result.output


def test_publish_refuses_a_suite_with_no_generated_tests(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = runner.invoke(app, ["publish", "--service", "svc", "--suite", str(tmp_path / "empty")])
    assert result.exit_code == 2 and "no generated tests" in result.output


class FailingPublisher:
    def publish(self, service: str, suite_dir: Path) -> str:
        raise RuntimeError("remote said no")


def test_a_publisher_that_fails_is_reported_as_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(BUILTIN_PUBLISHERS, "failing", FailingPublisher)
    suite = make_suite(tmp_path / "suite", "test_a.py")
    arguments = ["--service", "svc", "--suite", str(suite), "--publisher", "failing"]
    result = runner.invoke(app, ["publish", *arguments])
    assert result.exit_code == 2
    assert "publisher 'failing' failed: remote said no" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--service", "Bad Name"], "invalid service"),
        (["--service", "svc", "--suite", "MISSING"], "no generated tests in"),
        (["--service", "svc", "--suite", "SUITE", "--publisher", "nope"], "unknown publisher"),
    ],
)
def test_publish_reports_failures_as_exit_2(tmp_path: Path, args: list[str], message: str) -> None:
    suite = make_suite(tmp_path / "suite", "test_a.py")
    resolved = [
        a.replace("MISSING", str(tmp_path / "missing")).replace("SUITE", str(suite)) for a in args
    ]
    result = runner.invoke(app, ["publish", *resolved])
    assert result.exit_code == 2 and message in result.output
