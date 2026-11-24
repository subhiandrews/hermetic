"""Write or update a generated suite from a ServiceModel, touching only what changed."""

from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermetic.diffing import SuitePlan, plan_changes
from hermetic.diffing.manifest import (
    MAX_MANIFEST_BYTES,
    Manifest,
    ManifestEntry,
    file_name_for,
    load_manifest,
    manifest_text,
    save_manifest,
)
from hermetic.generate.author import AuthoredTest, AuthorRequest, SuiteAuthor
from hermetic.generate.validate import MAX_SOURCE_BYTES, SourceRejected, validate_source
from hermetic.history.models import SuiteChange
from hermetic.ingest import ServiceModel
from hermetic.ingest.models import Operation

MANIFEST_NAME = Path(".hermetic") / "manifest.json"
MAX_CONCURRENT_AUTHORS = 4


class GenerateError(Exception):
    """The suite could not be generated. Nothing on disk was changed."""


def fragment_text(operation: Operation) -> str:
    """The operation as indented JSON, without its hash, to show before/after in a run."""
    payload = operation.model_dump(mode="json", exclude={"fragment_hash"})
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)


def generate_suite(
    model: ServiceModel, author: SuiteAuthor, suite_dir: Path, *, force: bool = False
) -> list[SuiteChange]:
    """Bring the suite in `suite_dir` in line with the spec and return what changed.

    Every new test is authored and validated before any file is written, so an authoring or
    validation failure leaves the suite and the manifest as they were. A failure while writing
    (a full disk, say) can leave some files ahead of the manifest; the next run notices and
    rewrites them. The manifest lives in `<suite_dir>/.hermetic/manifest.json`.
    """
    manifest_path = suite_dir / MANIFEST_NAME
    manifest = load_manifest(manifest_path)
    plan = plan_changes(
        model,
        manifest,
        author.prompt_version,
        force=force,
        existing_files=_existing_files(suite_dir),
    )

    work: list[tuple[Operation, str, str | None]] = []  # operation, reason, previous source
    work.extend((operation, "a new operation in the spec", None) for operation in plan.added)
    for modification in plan.modified:
        previous = _read_previous(suite_dir, modification.previous)
        work.append((modification.operation, modification.reason, previous))

    # The manifest stores every fragment and cannot be read back above a size limit. Measure the
    # one this run would write, with the longest note `_reason` allows, so a spec that cannot fit
    # fails here, before the paid authoring step, and not after the files are written.
    worst = {op.operation_id: _reason(reason, _WORST_NOTE) for op, reason, _ in work}
    probe = Manifest(entries=_next_entries(manifest, plan, author.prompt_version, worst))
    needed = len(manifest_text(probe).encode("utf-8"))
    if needed > MAX_MANIFEST_BYTES:
        raise GenerateError(
            f"the manifest would need up to {needed} bytes, over its {MAX_MANIFEST_BYTES} byte "
            "limit; split the spec"
        )

    def author_one(item: tuple[Operation, str, str | None]) -> tuple[AuthoredTest, str]:
        operation, reason, previous = item
        request = AuthorRequest(
            operation=operation,
            service_title=model.title,
            servers=model.servers,
            docs=model.docs,
            reason=reason,
            previous_source=previous,
        )
        try:
            result = author.write_test(request)
            validate_source(result.source, operation.operation_id)
        except SourceRejected as exc:
            raise GenerateError(f"test for {operation.operation_id!r} rejected: {exc}") from exc
        except Exception as exc:  # the author may be a remote agent and can fail any way
            raise GenerateError(
                f"could not write a test for {operation.operation_id!r}: {exc}"
            ) from exc
        return result, reason

    # The model round trips are independent, so overlap them. Results are taken in spec order
    # and the first failure in that order is raised, after queued calls are cancelled so no
    # more paid requests start; nothing is written until every test is in hand. Calls already
    # running are not interrupted: leaving the block waits for them (each is bounded by the
    # author's timeout), so a failure is reported once they finish, not at the moment it happens.
    authored: dict[str, tuple[AuthoredTest, str]] = {}
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_AUTHORS) as pool:
        futures = [pool.submit(author_one, item) for item in work]
        try:
            for (operation, _, _), future in zip(work, futures, strict=True):
                authored[operation.operation_id] = future.result()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    changes: list[SuiteChange] = []
    for operation in plan.added:
        result, reason = authored[operation.operation_id]
        changes.append(
            SuiteChange(
                kind="added",
                test_id=file_name_for(operation.operation_id),
                reason=_reason(reason, result.note),
            )
        )
    for modification in plan.modified:
        result, reason = authored[modification.operation.operation_id]
        changes.append(
            SuiteChange(
                kind="modified",
                test_id=modification.previous.test_file,
                reason=_reason(reason, result.note),
                before=modification.previous.fragment,
                after=fragment_text(modification.operation),
            )
        )
    for entry in plan.dropped:
        changes.append(
            SuiteChange(
                kind="dropped",
                test_id=entry.test_file,
                reason="the operation was removed from the spec",
                before=entry.fragment,
            )
        )

    suite_dir.mkdir(parents=True, exist_ok=True)
    for operation in [*plan.added, *(m.operation for m in plan.modified)]:
        source = authored[operation.operation_id][0].source
        _write_text(suite_dir / file_name_for(operation.operation_id), source)
    for entry in plan.dropped:
        (suite_dir / entry.test_file).unlink(missing_ok=True)
    reasons = {key: _reason(reason, result.note) for key, (result, reason) in authored.items()}
    entries = _next_entries(manifest, plan, author.prompt_version, reasons)
    save_manifest(manifest_path, Manifest(entries=entries))
    return changes


# The agent's note goes into the manifest and the run record, so its length is bounded: the
# manifest's size can then be checked, in the worst case, before any model call is paid for.
MAX_NOTE_CHARS = 300
_WORST_NOTE = "\U0001f600" * MAX_NOTE_CHARS  # four bytes a character in UTF-8


def _reason(reason: str, note: str) -> str:
    note = " ".join(note.split())[:MAX_NOTE_CHARS]
    return f"{reason}: {note}" if note else reason


def _next_entries(
    manifest: Manifest, plan: SuitePlan, prompt_version: str, reasons: dict[str, str]
) -> dict[str, ManifestEntry]:
    """The manifest's entries once `plan` is applied; `reasons` maps operation id to its reason."""
    entries = dict(manifest.entries)
    for operation in [*plan.added, *(m.operation for m in plan.modified)]:
        entries[operation.operation_id] = ManifestEntry(
            operation_id=operation.operation_id,
            test_file=file_name_for(operation.operation_id),
            fragment_hash=operation.fragment_hash,
            fragment=fragment_text(operation),
            prompt_version=prompt_version,
            reason=reasons[operation.operation_id],
        )
    for entry in plan.dropped:
        del entries[entry.operation_id]
    return entries


def _existing_files(suite_dir: Path) -> frozenset[str]:
    """Names of the regular files in `suite_dir`; a symlink does not count as a test file."""
    if not suite_dir.is_dir():
        return frozenset()
    return frozenset(
        path.name for path in suite_dir.iterdir() if path.is_file() and not path.is_symlink()
    )


def _read_previous(suite_dir: Path, entry: ManifestEntry) -> str | None:
    path = suite_dir / entry.test_file
    if path.is_symlink():
        return None  # the file is committed and untrusted: never send what a link points at
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return None  # a test the validator would refuse is not sent to the model either
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # the file was deleted or edited into something unreadable: start afresh


def _write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gen-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; these files are committed and shared
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
