"""Builders for run records used across tests."""

from __future__ import annotations

from hermetic.history import RunRecord, TestResult


def make_record(run_id: str = "20260101T000000Z-aaaaaa", **overrides: object) -> RunRecord:
    fields: dict[str, object] = {
        "run_id": run_id,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "target": "http://svc",
        "spec_hash": "abc",
        "doc_hashes": {},
        "prompt_version": None,
        "tool_version": "0.0.1",
        "results": [],
    }
    fields.update(overrides)
    return RunRecord.model_validate(fields)


def make_result(status: str) -> TestResult:
    return TestResult.model_validate(
        {
            "test_id": f"t::{status}",
            "operation_id": None,
            "status": status,
            "duration_ms": 1.0,
            "message": None,
            "exchanges": [],
            "curl": None,
        }
    )
