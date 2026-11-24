"""Diff stage: compare the spec with the manifest and decide what to regenerate."""

from __future__ import annotations

from hermetic.diffing.manifest import (
    Manifest,
    ManifestEntry,
    ManifestError,
    load_manifest,
    save_manifest,
)
from hermetic.diffing.plan import Modification, SuitePlan, plan_changes

__all__ = [
    "Manifest",
    "ManifestEntry",
    "ManifestError",
    "Modification",
    "SuitePlan",
    "load_manifest",
    "plan_changes",
    "save_manifest",
]
