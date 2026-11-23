from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.history import FileRunStore
from hermetic.report import SinkError
from hermetic.runner import new_run_id, run_suite

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
