"""Opt-in: calls the real Claude API. Run with `uv run pytest -m live`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hermetic.agent import ClaudeAuthor
from hermetic.generate import AuthorRequest, MockSurface
from hermetic.generate.validate import validate_source
from hermetic.ingest import ingest
from hermetic.ingest.models import DocFile

SPEC = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


@pytest.mark.live
def test_claude_writes_a_valid_test() -> None:
    model = ingest(SPEC)
    operation = next(o for o in model.operations if o.operation_id == "getPetById")
    request = AuthorRequest(operation, model.title, model.servers, [], "a new operation", None)
    authored = ClaudeAuthor().write_test(request)
    validate_source(authored.source, "getPetById")


DESIGN = DocFile(
    path="design.md",
    kind="markdown",
    sha256="x",
    text=(
        "# Pet service\n\n"
        "`getPetById` asks the inventory service for the pet's stock with its `getStock` "
        "operation. If the inventory service answers 503, `getPetById` answers 503 too.\n"
    ),
)


def _takes_mocks(source: str) -> bool:
    """Whether the code uses `hermetic_mocks`; a mention in a comment or a string is not use."""
    tree = ast.parse(source)
    return any(isinstance(n, ast.arg) and n.arg == "hermetic_mocks" for n in ast.walk(tree)) or any(
        isinstance(n, ast.Name) and n.id == "hermetic_mocks" for n in ast.walk(tree)
    )


def _answered_operations(source: str) -> set[str]:
    """The operation names given to any `.answer(...)` call, as string literals."""
    return {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "answer"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _request(*, mocks: tuple[MockSurface, ...]) -> AuthorRequest:
    model = ingest(SPEC)
    operation = next(o for o in model.operations if o.operation_id == "getPetById")
    return AuthorRequest(
        operation, model.title, model.servers, [DESIGN], "a new operation", None, mocks=mocks
    )


@pytest.mark.live
def test_claude_switches_a_mock_the_docs_say_the_operation_calls() -> None:
    inventory = MockSurface("inventory", {"getStock": ["404", "503"]})
    authored = ClaudeAuthor().write_test(_request(mocks=(inventory,)))
    validate_source(authored.source, "getPetById")
    assert _takes_mocks(authored.source)
    assert "getStock" in _answered_operations(authored.source)


@pytest.mark.live
def test_claude_leaves_the_fixture_alone_without_a_mocks_block() -> None:
    authored = ClaudeAuthor().write_test(_request(mocks=()))
    validate_source(authored.source, "getPetById")
    assert not _takes_mocks(authored.source)
