from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermetic.diffing.manifest import ManifestEntry, file_name_for, load_manifest, save_manifest
from hermetic.generate import GenerateError, Manifest, ManifestError, generate_suite
from hermetic.generate.suite import MANIFEST_NAME, fragment_text
from hermetic.ingest import ServiceModel, ingest
from tests.fakes import FakeAuthor, good_source

SPEC = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


def model_from(tmp_path: Path, edit: Any = None, docs: Path | None = None) -> ServiceModel:
    spec = json.loads(SPEC.read_text())
    if edit:
        edit(spec)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return ingest(path, docs)


def test_first_generation_adds_every_operation(tmp_path: Path) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    changes = generate_suite(model, FakeAuthor(), suite)
    assert {c.kind for c in changes} == {"added"}
    assert len(changes) == len(model.operations)
    assert changes[0].reason == "a new operation in the spec: covers the success case"
    manifest = load_manifest(suite / MANIFEST_NAME)
    assert set(manifest.entries) == {o.operation_id for o in model.operations}
    assert all((suite / e.test_file).exists() for e in manifest.entries.values())


def test_second_run_changes_nothing_and_asks_the_agent_nothing(tmp_path: Path) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    generate_suite(model, FakeAuthor(), suite)
    author = FakeAuthor()
    assert generate_suite(model, author, suite) == []
    assert author.requests == []


def test_spec_edit_modifies_only_that_operation(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    generate_suite(model_from(tmp_path), FakeAuthor(), suite)

    def edit(spec: dict[str, Any]) -> None:
        spec["paths"]["/pet/{petId}"]["get"]["summary"] = "Find a pet, changed"

    author = FakeAuthor(note="asserts the new summary")
    changes = generate_suite(model_from(tmp_path, edit), author, suite)
    (change,) = changes
    assert change.kind == "modified"
    assert change.reason == "the operation changed in the spec: asserts the new summary"
    assert change.before is not None and "Find a pet, changed" not in change.before
    assert change.after is not None and "Find a pet, changed" in change.after
    (request,) = author.requests
    assert request.operation.operation_id == "getPetById"
    assert request.previous_source == good_source("getPetById")


def test_removed_operation_is_dropped_and_its_file_deleted(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    generate_suite(model_from(tmp_path), FakeAuthor(), suite)

    def edit(spec: dict[str, Any]) -> None:
        del spec["paths"]["/pet/{petId}"]["delete"]

    (change,) = generate_suite(model_from(tmp_path, edit), FakeAuthor(), suite)
    assert change.kind == "dropped" and change.test_id == file_name_for("deletePet")
    assert change.reason == "the operation was removed from the spec"
    assert not (suite / file_name_for("deletePet")).exists()
    assert "deletePet" not in load_manifest(suite / MANIFEST_NAME).entries


def test_new_prompt_version_and_force_rewrite_tests(tmp_path: Path) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    generate_suite(model, FakeAuthor("1"), suite)
    changes = generate_suite(model, FakeAuthor("2"), suite)
    assert {c.kind for c in changes} == {"modified"}
    assert "the generation prompt changed (1 -> 2)" in changes[0].reason
    forced = generate_suite(model, FakeAuthor("2"), suite, force=True)
    assert len(forced) == len(model.operations)
    assert "regeneration was requested" in forced[0].reason


def test_empty_note_leaves_the_reason_alone(tmp_path: Path) -> None:
    changes = generate_suite(model_from(tmp_path), FakeAuthor(note=" "), tmp_path / "s")
    assert changes[0].reason == "a new operation in the spec"


def test_rejected_source_changes_nothing_on_disk(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    author = FakeAuthor()
    author.source_for["addPet"] = "import os\n"
    with pytest.raises(GenerateError, match="'addPet' rejected: importing `os`"):
        generate_suite(model_from(tmp_path), author, suite)
    assert not suite.exists()


def test_author_failure_is_a_generate_error(tmp_path: Path) -> None:
    author = FakeAuthor()
    author.fail_on = "addPet"
    with pytest.raises(GenerateError, match="could not write a test for 'addPet'"):
        generate_suite(model_from(tmp_path), author, tmp_path / "suite")


def test_missing_previous_file_means_no_previous_source(tmp_path: Path) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    generate_suite(model, FakeAuthor(), suite)
    (suite / file_name_for("addPet")).unlink()
    (suite / file_name_for("getPetById")).write_bytes(b"\xff\xfe")
    author = FakeAuthor()
    generate_suite(model, author, suite, force=True)
    previous = {r.operation.operation_id: r.previous_source for r in author.requests}
    assert previous["addPet"] is None and previous["getPetById"] is None
    assert previous["deletePet"] == good_source("deletePet")


def test_a_symlinked_test_file_is_never_read_into_the_prompt(tmp_path: Path) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    generate_suite(model, FakeAuthor(), suite)
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY")
    target = suite / file_name_for("addPet")
    target.unlink()
    target.symlink_to(secret)
    author = FakeAuthor()
    generate_suite(model, author, suite)  # the symlink counts as a missing test file
    (request,) = author.requests
    assert request.operation.operation_id == "addPet" and request.previous_source is None
    assert not target.is_symlink() and "PRIVATE" not in target.read_text()
    assert secret.read_text() == "PRIVATE KEY"


def test_written_files_are_not_owner_only(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    generate_suite(model_from(tmp_path), FakeAuthor(), suite)
    written = [suite / MANIFEST_NAME, *suite.glob("test_*.py")]
    assert len(written) > 1
    assert {path.stat().st_mode & 0o777 for path in written} == {0o644}


def test_an_oversized_previous_test_is_not_sent_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    generate_suite(model, FakeAuthor(), suite)
    monkeypatch.setattr("hermetic.generate.suite.MAX_SOURCE_BYTES", 10)
    author = FakeAuthor()
    generate_suite(model, author, suite, force=True)
    assert author.requests and all(r.previous_source is None for r in author.requests)


def test_a_deleted_test_file_is_regenerated(tmp_path: Path) -> None:
    model = model_from(tmp_path)
    suite = tmp_path / "suite"
    generate_suite(model, FakeAuthor(), suite)
    (suite / file_name_for("addPet")).unlink()
    author = FakeAuthor()
    (change,) = generate_suite(model, author, suite)
    assert change.kind == "modified"
    assert change.reason.startswith("its test file was missing")
    assert (suite / file_name_for("addPet")).exists()


def test_authoring_runs_concurrently_and_reports_the_first_failure_in_spec_order(
    tmp_path: Path,
) -> None:
    model = model_from(tmp_path)
    first, second, *_ = (o.operation_id for o in model.operations)
    author = FakeAuthor()
    author.source_for[first] = "import os\n"
    author.source_for[second] = "import sys\n"
    with pytest.raises(GenerateError, match=f"'{first}' rejected: importing `os`"):
        generate_suite(model, author, tmp_path / "suite")
    assert not (tmp_path / "suite").exists()


def test_unwritable_target_cleans_up_its_temp_file(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / file_name_for("addPet")).mkdir()  # os.replace onto a directory fails
    with pytest.raises(OSError):
        generate_suite(model_from(tmp_path), FakeAuthor(), suite)
    assert not list(suite.glob(".gen-*"))


def test_file_names_do_not_collide_and_are_safe() -> None:
    assert file_name_for("a-b") != file_name_for("a_b")
    assert file_name_for("../../x y").startswith("test____")
    assert "/" not in file_name_for("../../x")


def test_fragment_text_omits_the_hash(tmp_path: Path) -> None:
    operation = model_from(tmp_path).operations[0]
    assert "fragment_hash" not in fragment_text(operation)


def entry(**over: str) -> ManifestEntry:
    fields = {
        "operation_id": "op",
        "fragment_hash": "h",
        "fragment": "{}",
        "prompt_version": "1",
        "reason": "r",
    }
    fields.update(over)
    fields.setdefault("test_file", file_name_for(fields["operation_id"]))
    return ManifestEntry.model_validate(fields)


def test_manifest_round_trips_sorted(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "manifest.json"
    manifest = Manifest(entries={"b": entry(operation_id="b"), "a": entry(operation_id="a")})
    save_manifest(path, manifest)
    assert list(json.loads(path.read_text())["entries"]) == ["a", "b"]
    assert load_manifest(path) == manifest


def test_missing_manifest_is_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "none.json") == Manifest()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "is not valid"),
        ('{"version": 2, "entries": {}}', "has version 2"),
        (json.dumps({"entries": {"x": entry().model_dump()}}), "invalid entry for 'x'"),
        (
            json.dumps({"entries": {"op": entry(test_file="../evil.py").model_dump()}}),
            "invalid entry for 'op'",
        ),
        (  # a well-formed name that is not the one computed for this operation
            json.dumps({"entries": {"op": entry(test_file="test_other_12345678.py").model_dump()}}),
            "invalid entry for 'op'",
        ),
    ],
)
def test_bad_manifests_are_rejected(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(content)
    with pytest.raises(ManifestError, match=message):
        load_manifest(path)


def test_oversized_and_unreadable_manifests_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    monkeypatch.setattr("hermetic.diffing.manifest.MAX_MANIFEST_BYTES", 1)
    with pytest.raises(ManifestError, match="larger than"):
        load_manifest(path)
    plain_file = tmp_path / "plain"
    plain_file.write_text("x")
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest(plain_file / "manifest.json")


def test_save_cleans_up_when_the_write_fails(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    target.mkdir()
    with pytest.raises(OSError):
        save_manifest(target, Manifest())
    assert not list(tmp_path.glob(".manifest-*"))


def test_a_deeply_nested_manifest_is_rejected_not_a_traceback(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("[" * 100_000)
    with pytest.raises(ManifestError, match="not valid"):
        load_manifest(path)
