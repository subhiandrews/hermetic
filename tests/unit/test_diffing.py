from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hermetic.diffing import plan_changes
from hermetic.diffing.manifest import ManifestEntry
from hermetic.generate import Manifest
from hermetic.ingest import ingest

SPEC = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


def test_classifies_every_operation() -> None:
    model = ingest(SPEC)
    first, second, third, *rest = model.operations

    def entry(op_id: str, fragment_hash: str, prompt: str = "1") -> ManifestEntry:
        return ManifestEntry(
            operation_id=op_id,
            test_file=f"test_{op_id}.py",
            fragment_hash=fragment_hash,
            fragment="{}",
            prompt_version=prompt,
            reason="r",
        )

    entries = {
        first.operation_id: entry(first.operation_id, first.fragment_hash),
        second.operation_id: entry(second.operation_id, "stale"),
        third.operation_id: entry(third.operation_id, third.fragment_hash, prompt="0"),
        "gone": entry("gone", "x"),
        "also_gone": entry("also_gone", "x"),
    }
    plan = plan_changes(model, Manifest(entries=entries), "1")
    assert plan.unchanged == [first.operation_id]
    assert [m.operation.operation_id for m in plan.modified] == [
        second.operation_id,
        third.operation_id,
    ]
    assert plan.modified[0].reason == "the operation changed in the spec"
    assert plan.modified[1].reason == "the generation prompt changed (0 -> 1)"
    assert len(plan.added) == len(rest)
    assert [e.operation_id for e in plan.dropped] == ["also_gone", "gone"]


def test_a_missing_test_file_is_regenerated_only_when_files_are_given() -> None:
    model = ingest(SPEC)
    first, second, *_ = model.operations
    entries = {
        op.operation_id: ManifestEntry(
            operation_id=op.operation_id,
            test_file=f"test_{op.operation_id}.py",
            fragment_hash=op.fragment_hash,
            fragment="{}",
            prompt_version="1",
            reason="r",
        )
        for op in (first, second)
    }
    manifest = Manifest(entries=entries)
    assert first.operation_id in plan_changes(model, manifest, "1").unchanged
    present = frozenset({f"test_{first.operation_id}.py"})
    plan = plan_changes(model, manifest, "1", existing_files=present)
    assert plan.unchanged == [first.operation_id]
    (modified,) = plan.modified
    assert modified.operation.operation_id == second.operation_id
    assert modified.reason == "its test file was missing"


def test_diffing_imports_first_without_a_cycle() -> None:
    done = subprocess.run(
        [sys.executable, "-c", "import hermetic.diffing, hermetic.generate"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
