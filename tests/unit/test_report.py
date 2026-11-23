from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from hermetic.history import RunRecord, SuiteChange
from hermetic.report import BUILTIN_SINKS, SinkContext, SinkError, load_sinks
from hermetic.report.html_sink import render_html
from hermetic.report.markdown import render_markdown
from tests.factories import make_record


def failing_record() -> RunRecord:
    result = {
        "test_id": "test_api.py::test_<b>",
        "operation_id": "getPet",
        "status": "failed",
        "duration_ms": 2.0,
        "message": "assert 500 == 200\n```injected```",
        "exchanges": [
            {
                "method": "GET",
                "url": "http://svc/x",
                "request_headers": {},
                "request_body": None,
                "status_code": None,
                "response_headers": {},
                "response_body": None,
                "duration_ms": 0.0,
            },
            {
                "method": "GET",
                "url": "http://svc/pets/1",
                "request_headers": {},
                "request_body": None,
                "status_code": 500,
                "response_headers": {},
                "response_body": "<script>alert(1)</script>",
                "duration_ms": 1.0,
            },
        ],
        "curl": "curl -sS -i -X GET http://svc/x",
    }
    passing = {**result, "test_id": "t::ok", "status": "passed", "message": None, "curl": None}
    return make_record(
        "r1",
        results=[
            result,
            passing,
            {**result, "status": "error", "operation_id": None, "curl": None, "message": None},
        ],
        changes=[SuiteChange(kind="added", test_id="t::new", reason="new <op>")],
    )


def test_markdown_lists_failures_with_repro_and_changes() -> None:
    text = render_markdown(failing_record())
    assert "Passed: 1, failed: 1, errors: 1" in text
    assert "### test_api.py::test_<b> (failed)" in text
    assert "Operation: `getPet`" in text
    assert "curl -sS -i -X GET http://svc/x" in text
    assert "- Suite changes: 1 added, 0 modified, 0 dropped" in text
    assert "- added: t::new: new <op>" in text
    # a backtick run inside the message must not close the fence early
    assert "````\nassert 500 == 200\n```injected```\n````" in text


def test_markdown_for_a_clean_run_has_no_failure_section() -> None:
    text = render_markdown(make_record())
    assert "## Failures" not in text and "Suite changes" not in text


def test_html_escapes_everything_recorded() -> None:
    page = render_html(failing_record())
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "test_&lt;b&gt;" in page
    assert "new &lt;op&gt;" in page
    assert "http://svc/pets/1 &rarr; 500" in page
    assert "http://svc/x &rarr;" not in page  # only the last exchange is shown per failure


def test_html_for_a_clean_run_has_no_failure_section() -> None:
    page = render_html(make_record())
    assert "Failures" not in page and "Suite changes" not in page


def test_html_shows_missing_response() -> None:
    record = failing_record()
    result = record.results[0].model_copy(update={"exchanges": record.results[0].exchanges[:1]})
    page = render_html(record.model_copy(update={"results": [result]}))
    assert "no response" in page


def test_file_sinks_write_one_file_per_run(tmp_path: Path) -> None:
    out = tmp_path / "reports"
    sinks = load_sinks(["markdown", "html", "json"], SinkContext(output_dir=out))
    for sink in sinks:
        sink.publish(failing_record())
    assert sorted(p.name for p in out.iterdir()) == ["r1.html", "r1.json", "r1.md"]
    assert RunRecord.model_validate_json((out / "r1.json").read_text()) == failing_record()


def test_stdout_sink_prints_markdown(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (sink,) = load_sinks(["stdout"], SinkContext(output_dir=tmp_path))
    sink.publish(make_record("r9"))
    assert "# Hermetic run r9" in capsys.readouterr().out


def test_builtin_names() -> None:
    assert set(BUILTIN_SINKS) == {"markdown", "html", "json", "stdout"}


class RecordingSink:
    published: list[str] = []  # noqa: RUF012

    def __init__(self, context: SinkContext) -> None:
        self.context = context

    def publish(self, record: RunRecord) -> None:
        RecordingSink.published.append(record.run_id)


def _entry_points(*eps: EntryPoint) -> object:
    return lambda group: [ep for ep in eps if ep.group == group]


def test_third_party_sinks_load_through_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = EntryPoint("custom", f"{__name__}:RecordingSink", "hermetic.sinks")
    monkeypatch.setattr("hermetic.report.sinks.entry_points", _entry_points(ep))
    (sink,) = load_sinks(["custom"], SinkContext(output_dir=tmp_path))
    sink.publish(make_record("viaplugin"))
    assert RecordingSink.published == ["viaplugin"]


def test_a_third_party_sink_cannot_replace_a_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = EntryPoint("json", f"{__name__}:RecordingSink", "hermetic.sinks")
    monkeypatch.setattr("hermetic.report.sinks.entry_points", _entry_points(ep))
    (sink,) = load_sinks(["json"], SinkContext(output_dir=tmp_path))
    assert type(sink) is BUILTIN_SINKS["json"]


def test_a_sink_that_fails_to_import_is_a_sink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = EntryPoint("broken", "no_such_module_xyz:Sink", "hermetic.sinks")
    monkeypatch.setattr("hermetic.report.sinks.entry_points", _entry_points(ep))
    with pytest.raises(SinkError, match="failed to load"):
        load_sinks(["broken"], SinkContext(output_dir=tmp_path))


def test_unknown_sink_lists_the_available_ones(tmp_path: Path) -> None:
    with pytest.raises(SinkError, match=r"unknown sink 'nope' \(available: html, json"):
        load_sinks(["nope"], SinkContext(output_dir=tmp_path))
