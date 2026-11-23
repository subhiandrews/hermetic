"""Opt-in: calls the real Claude API. Run with `uv run pytest -m live`."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermetic.agent import ClaudeAuthor
from hermetic.generate import AuthorRequest
from hermetic.generate.validate import validate_source
from hermetic.ingest import ingest

SPEC = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


@pytest.mark.live
def test_claude_writes_a_valid_test() -> None:
    model = ingest(SPEC)
    operation = next(o for o in model.operations if o.operation_id == "getPetById")
    request = AuthorRequest(operation, model.title, model.servers, [], "a new operation", None)
    authored = ClaudeAuthor().write_test(request)
    validate_source(authored.source, "getPetById")
