"""Decide which operations need a new test, a rewritten test, or no test."""

from __future__ import annotations

from dataclasses import dataclass

from hermetic.diffing.manifest import Manifest, ManifestEntry
from hermetic.ingest import ServiceModel
from hermetic.ingest.models import Operation


@dataclass(frozen=True)
class Modification:
    operation: Operation
    previous: ManifestEntry
    reason: str


@dataclass(frozen=True)
class SuitePlan:
    added: list[Operation]
    modified: list[Modification]
    dropped: list[ManifestEntry]
    unchanged: list[str]  # operation ids whose test is kept as it is


def plan_changes(
    model: ServiceModel,
    manifest: Manifest,
    prompt_version: str,
    *,
    force: bool = False,
    existing_files: frozenset[str] | None = None,
) -> SuitePlan:
    """Classify every operation against the manifest, in spec order.

    A test is rewritten when the operation's fragment hash changed (which includes any shared
    schema it uses), when the prompt version changed, when its test file is not among
    `existing_files` (None skips that check), or when `force` is set. A change to the docs
    alone does not rewrite tests: docs are not tied to one operation, so use `force`.
    """
    added: list[Operation] = []
    modified: list[Modification] = []
    unchanged: list[str] = []
    current: set[str] = set()
    for operation in model.operations:
        current.add(operation.operation_id)
        entry = manifest.entries.get(operation.operation_id)
        if entry is None:
            added.append(operation)
        elif entry.fragment_hash != operation.fragment_hash:
            modified.append(Modification(operation, entry, "the operation changed in the spec"))
        elif entry.prompt_version != prompt_version:
            reason = f"the generation prompt changed ({entry.prompt_version} -> {prompt_version})"
            modified.append(Modification(operation, entry, reason))
        elif existing_files is not None and entry.test_file not in existing_files:
            modified.append(Modification(operation, entry, "its test file was missing"))
        elif force:
            modified.append(Modification(operation, entry, "regeneration was requested"))
        else:
            unchanged.append(operation.operation_id)
    dropped = [
        entry
        for operation_id, entry in sorted(manifest.entries.items())
        if operation_id not in current
    ]
    return SuitePlan(added, modified, dropped, unchanged)
