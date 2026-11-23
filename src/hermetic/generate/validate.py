"""Deterministic checks on agent-written test source, before it is written to disk.

The suite runs in-process with the user's privileges, so these checks are a guardrail against
a confused or manipulated agent, not a sandbox: only a small set of imports is allowed, and
a few dangerous builtins may not be called or referenced.
"""

from __future__ import annotations

import ast

ALLOWED_IMPORTS = frozenset(
    {
        "base64",
        "datetime",
        "decimal",
        "json",
        "math",
        "pytest",
        "re",
        "string",
        "time",
        "typing",
        "uuid",
    }
)
FORBIDDEN_NAMES = frozenset(
    {
        "breakpoint", "compile", "delattr", "eval", "exec", "getattr", "globals", "importorskip",
        "locals", "open", "setattr", "type", "vars",
    }
)  # fmt: skip
# Attributes that reach the interpreter or the system from an allowed module (`typing.sys`).
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "builtins", "import_module", "importorskip", "modules", "os", "popen", "subprocess",
        "sys", "system",
    }
)  # fmt: skip
# Fixtures that give a test the file system or the environment; only `hermetic_client` is meant.
FORBIDDEN_FIXTURES = frozenset(
    {"cache", "capfd", "capsys", "monkeypatch", "pytestconfig", "request", "tmp_path",
     "tmp_path_factory", "tmpdir", "tmpdir_factory"}
)  # fmt: skip
MAX_SOURCE_BYTES = 256 * 1024


class SourceRejected(Exception):
    """The source is not an acceptable test module."""


def validate_source(source: str, operation_id: str) -> None:
    """Raise SourceRejected unless `source` is a safe module testing `operation_id`."""
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise SourceRejected(f"the test is larger than {MAX_SOURCE_BYTES} bytes")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise SourceRejected(f"the test is not valid Python: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                raise SourceRejected("relative imports are not allowed")
            _check_module(node.module)
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES | FORBIDDEN_ATTRIBUTES:
                    raise SourceRejected(f"importing `{alias.name}` is not allowed in a test")
        elif isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES or node.id.startswith("__")
        ):
            raise SourceRejected(f"`{node.id}` is not allowed in a test")
        elif isinstance(node, ast.Attribute) and (
            node.attr in FORBIDDEN_ATTRIBUTES
            or node.attr in FORBIDDEN_NAMES
            or node.attr.startswith("__")
        ):
            raise SourceRejected(f"attribute `{node.attr}` is not allowed in a test")
        elif isinstance(node, ast.arg) and node.arg in FORBIDDEN_FIXTURES:
            raise SourceRejected(f"fixture `{node.arg}` is not allowed in a test")
    tests = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    ]
    if not tests:
        raise SourceRejected("the module defines no top-level test_ function")
    for function in tests:
        if not any(_marks_operation(d, operation_id) for d in function.decorator_list):
            raise SourceRejected(f"{function.name} lacks @pytest.mark.operation({operation_id!r})")


def _check_module(name: str) -> None:
    if name.split(".")[0] not in ALLOWED_IMPORTS:
        raise SourceRejected(f"importing `{name}` is not allowed in a test")


def _marks_operation(decorator: ast.expr, operation_id: str) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == "pytest.mark.operation"
        and len(decorator.args) == 1
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == operation_id
    )
