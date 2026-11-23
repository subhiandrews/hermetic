from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.history import FileRunStore, SuiteChange
from hermetic.report import SinkError
from hermetic.runner import new_run_id, run_suite
from tests.fakes import FakeAuthor

runner = CliRunner()
SPEC = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


def write_suite(tmp_path: Path, assertion: str) -> Path:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_api.py").write_text(
        "def test_a(hermetic_client):\n"
        f"    assert hermetic_client.get('/ok').status_code {assertion}\n"
    )
    return suite


def test_run_passes_records_and_prints_a_report(server: str, tmp_path: Path) -> None:
    suite = write_suite(tmp_path, "== 200")
    history = tmp_path / "history"
    result = runner.invoke(
        app, ["run", str(suite), "--target", server, "--history-dir", str(history)]
    )
    assert result.exit_code == 0, result.output
    assert "Passed: 1, failed: 0" in result.output
    (summary,) = FileRunStore(history).list()
    assert summary.passed == 1 and summary.spec_hash is None


def test_run_with_failures_exits_1_and_writes_reports(server: str, tmp_path: Path) -> None:
    suite = write_suite(tmp_path, "== 500")
    reports = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "run", str(suite), "--target", server, "--history-dir", str(tmp_path / "h"),
            "--report-dir", str(reports), "--report", "markdown", "--report", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert sorted(p.suffix for p in reports.iterdir()) == [".json", ".md"]


def test_run_with_spec_records_hashes(server: str, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "design.md").write_text("# Design\n")
    history = tmp_path / "h"
    result = runner.invoke(
        app,
        [
            "run", str(write_suite(tmp_path, "== 200")), "--target", server,
            "--spec", str(SPEC), "--docs", str(docs), "--history-dir", str(history),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    (summary,) = FileRunStore(history).list()
    record = FileRunStore(history).get(summary.run_id)
    assert summary.spec_hash and list(record.doc_hashes) == ["design.md"]
    assert json.loads((history / "index.json").read_text())[0]["run_id"] == summary.run_id


def test_run_errors_exit_2(server: str, tmp_path: Path) -> None:
    suite = write_suite(tmp_path, "== 200")
    base = ["run", str(suite), "--target", server, "--history-dir", str(tmp_path / "h")]
    for extra, message in (
        (["--report", "nope"], "unknown sink"),
        (["--spec", str(tmp_path / "missing.json")], "error:"),
    ):
        result = runner.invoke(app, base + extra)
        assert result.exit_code == 2 and message in result.output
    missing = runner.invoke(app, ["run", str(tmp_path / "none"), "--target", server])
    assert missing.exit_code == 2 and "suite not found" in missing.output


def test_run_store_failure_exits_2(server: str, tmp_path: Path) -> None:
    blocker = tmp_path / "history"
    blocker.write_text("a file")
    result = runner.invoke(
        app,
        [
            "run",
            str(write_suite(tmp_path, "== 200")),
            "--target",
            server,
            "--history-dir",
            str(blocker),
        ],
    )
    assert result.exit_code == 2 and "cannot write run" in result.output


def test_run_ids_sort_by_time_and_do_not_collide() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    first, second = new_run_id(now), new_run_id(now)
    assert first.startswith("20260102T030405Z-") and first != second


def test_run_suite_publishes_after_saving(server: str, tmp_path: Path) -> None:
    order: list[str] = []

    class Store:
        def save(self, record: object) -> None:
            order.append("save")

    class Sink:
        def publish(self, record: object) -> None:
            order.append("publish")

    run_suite(write_suite(tmp_path, "== 200"), server, Store(), [Sink()])  # type: ignore[arg-type]
    assert order == ["save", "publish"]


def test_docs_without_spec_is_a_usage_error(server: str, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", str(write_suite(tmp_path, "== 200")), "--target", server, "--docs", str(tmp_path)],
    )
    assert result.exit_code == 2 and "--docs needs --spec" in result.output


def test_run_target_is_stored_without_credentials(server: str, tmp_path: Path) -> None:
    userinfo_target = server.replace("http://", "http://user:pw@")
    result = runner.invoke(
        app,
        [
            "run", str(write_suite(tmp_path, "== 200")), "--target", userinfo_target,
            "--history-dir", str(tmp_path / "h"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "pw" not in (tmp_path / "h" / "index.json").read_text()


def test_every_sink_runs_even_if_one_fails(server: str, tmp_path: Path) -> None:
    published: list[str] = []

    class Boom:
        def publish(self, record: object) -> None:
            raise OSError("read-only")

    class Fine:
        def publish(self, record: object) -> None:
            published.append("fine")

    class Store:
        def save(self, record: object) -> None:
            published.append("saved")

    suite = write_suite(tmp_path, "== 200")
    with pytest.raises(SinkError, match=r"was saved, but publishing failed: Boom: read-only"):
        run_suite(suite, server, Store(), [Boom(), Fine()])  # type: ignore[arg-type]
    assert published == ["saved", "fine"]


def test_sink_failure_still_exits_1_when_tests_failed(server: str, tmp_path: Path) -> None:
    unwritable = tmp_path / "reports"
    unwritable.write_text("a file where the directory should be")
    args = [
        "--target",
        server,
        "--history-dir",
        str(tmp_path / "h"),
        "--report-dir",
        str(unwritable),
    ]
    failing = runner.invoke(
        app, ["run", str(write_suite(tmp_path, "== 500")), *args, "--report", "json"]
    )
    assert failing.exit_code == 1 and "was saved, but publishing failed" in failing.output
    passing_suite = tmp_path / "ok"
    passing_suite.mkdir()
    (passing_suite / "test_ok.py").write_text("def test_ok():\n    pass\n")
    passing = runner.invoke(app, ["run", str(passing_suite), *args, "--report", "json"])
    assert passing.exit_code == 2


def test_store_failure_after_the_run_still_exits_1_when_tests_failed(
    server: str, tmp_path: Path
) -> None:
    blocker = tmp_path / "history"
    blocker.write_text("a file")
    failing = runner.invoke(
        app,
        [
            "run", str(write_suite(tmp_path, "== 500")), "--target", server,
            "--history-dir", str(blocker),
        ],
    )  # fmt: skip
    assert failing.exit_code == 1 and "cannot write run" in failing.output


def use_fake_author(monkeypatch: pytest.MonkeyPatch, author: object) -> None:
    monkeypatch.setattr("hermetic.cli.make_author", lambda: author)


def test_generate_command_writes_the_suite_and_lists_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_author(monkeypatch, FakeAuthor())
    suite = tmp_path / "suite"
    result = runner.invoke(app, ["generate", str(SPEC), "--suite", str(suite)])
    assert result.exit_code == 0, result.output
    assert "added: test_addPet_" in result.output
    again = runner.invoke(app, ["generate", str(SPEC), "--suite", str(suite)])
    assert again.output.strip() == "suite is up to date"
    forced = runner.invoke(app, ["generate", str(SPEC), "--suite", str(suite), "--force"])
    assert "modified: test_addPet_" in forced.output


def test_generate_command_reports_failures_as_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = FakeAuthor()
    author.fail_on = "addPet"
    use_fake_author(monkeypatch, author)
    result = runner.invoke(app, ["generate", str(SPEC), "--suite", str(tmp_path / "s")])
    assert result.exit_code == 2 and "could not write a test" in result.output
    missing = runner.invoke(app, ["generate", str(tmp_path / "no.json"), "--suite", str(tmp_path)])
    assert missing.exit_code == 2


def test_a_bad_report_name_fails_before_any_paid_generation(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = FakeAuthor()
    use_fake_author(monkeypatch, author)
    suite = tmp_path / "suite"
    result = runner.invoke(
        app,
        [
            "run", str(suite), "--target", server, "--spec", str(SPEC),
            "--generate", "--report", "nope",
        ],
    )  # fmt: skip
    assert result.exit_code == 2 and "unknown sink" in result.output
    assert author.requests == [] and not suite.exists()


def test_run_generate_records_the_changes(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_author(monkeypatch, FakeAuthor("7"))
    history = tmp_path / "h"
    result = runner.invoke(
        app,
        [
            "run", str(tmp_path / "suite"), "--target", server, "--spec", str(SPEC),
            "--generate", "--history-dir", str(history),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    (summary,) = FileRunStore(history).list()
    assert summary.added == 19 and summary.passed == 19
    assert FileRunStore(history).get(summary.run_id).prompt_version == "7"


def test_run_generate_needs_a_spec_and_a_working_author(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_spec = runner.invoke(app, ["run", str(tmp_path), "--target", server, "--generate"])
    assert no_spec.exit_code == 2 and "--generate needs --spec" in no_spec.output
    author = FakeAuthor()
    author.fail_on = "addPet"
    use_fake_author(monkeypatch, author)
    broken = runner.invoke(
        app, ["run", str(tmp_path / "s"), "--target", server, "--spec", str(SPEC), "--generate"]
    )
    assert broken.exit_code == 2 and "could not write a test" in broken.output


def test_run_records_given_changes_and_prompt_version(server: str, tmp_path: Path) -> None:
    change = SuiteChange(kind="added", test_id="t", reason="r")
    store = FileRunStore(tmp_path / "h")
    record = run_suite(
        write_suite(tmp_path, "== 200"), server, store, [], changes=[change], prompt_version="3"
    )
    assert record.changes == [change] and record.prompt_version == "3"


def test_write_errors_are_reported_not_raised(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_author(monkeypatch, FakeAuthor())
    blocker = tmp_path / "plain-file"
    blocker.write_text("x")
    gen = runner.invoke(app, ["generate", str(SPEC), "--suite", str(blocker / "suite")])
    assert gen.exit_code == 2 and "error:" in gen.output
    run = runner.invoke(
        app, ["run", str(blocker / "s"), "--target", server, "--spec", str(SPEC), "--generate"]
    )
    assert run.exit_code == 2 and "error:" in run.output
