"""Deterministic checks on agent-written test source, before it is written to disk.

The suite runs in-process with the user's privileges, so these checks are a guardrail against
a confused or manipulated agent, not a sandbox: only a small set of imports is allowed, and
a few dangerous builtins may not be called or referenced.
"""

from __future__ import annotations

import ast
import re

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
        "breakpoint", "compile", "delattr", "eval", "exec", "getattr", "globals", "help",
        "importorskip", "locals", "open", "setattr", "type", "usefixtures", "vars",
    }
)  # fmt: skip
# Attributes that reach the interpreter or the system from an allowed module (`typing.sys`).
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "builtins", "import_module", "importorskip", "modules", "os", "popen", "subprocess",
        "sys", "system",
        # Ways to reach the client's origin check, or to evaluate strings by name.
        "event_hooks", "base_url", "_transport", "_mounts",
        "ForwardRef", "Formatter", "get_type_hints", "_evaluate",
        # Frame and traceback objects expose `f_globals`, and so the real builtins.
        "gi_frame", "cr_frame", "ag_frame", "f_globals", "f_locals", "f_builtins", "f_back",
        "tb_frame",
        # Modules an allowed module re-exports (`typing.operator`), and ways to start another
        # pytest session or get a fixture by hand (`pytest.main`, `pytest.MonkeyPatch`).
        "functools", "operator", "types", "attrgetter", "itemgetter", "methodcaller",
        "MonkeyPatch", "Pytester", "main", "console_main",
        # Requests a fixture by name, bypassing the FORBIDDEN_FIXTURES check on arguments.
        "usefixtures",
        # File access through a module an allowed one imports (`json.codecs.open`).
        "codecs", "io", "open",
    }
)  # fmt: skip
# `pytest.mark.skipif("...")` and `xfail("...")` eval() a string condition at collection.
# They are only allowed as a direct call whose arguments are all non-string constants.
EVALUATING_MARKS = frozenset({"skipif", "xfail"})
FORMAT_METHODS = frozenset({"format", "format_map"})
# `"{0.__class__}".format(x)` reaches dunder attributes by name, without an Attribute node.
_FORMAT_DUNDER = re.compile(r"\{[^{}]*__")
# Fixtures that give a test the file system or the environment; only `hermetic_client` is meant.
FORBIDDEN_FIXTURES = frozenset(
    {"cache", "capfd", "capsys", "monkeypatch", "pytestconfig", "pytester", "request", "testdir",
     "tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"}
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
    safe_marks: set[int] = set()  # skipif/xfail nodes used as the callee of a harmless call
    safe_formats: set[int] = set()  # `.format` nodes called directly on a string literal
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                raise SourceRejected("relative imports are not allowed")
            _check_module(node.module)
            for alias in node.names:
                if (
                    alias.name == "*"
                    or alias.name.startswith("_")  # private helpers: `uuid._get_command_stdout`
                    or alias.name in FORBIDDEN_NAMES | FORBIDDEN_ATTRIBUTES | EVALUATING_MARKS
                ):
                    raise SourceRejected(f"importing `{alias.name}` is not allowed in a test")
        elif isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES
            or node.id in EVALUATING_MARKS
            or node.id == "usefixtures"
            or node.id.startswith(("__", "pytest_"))  # `pytest_plugins` loads any plugin
        ):
            raise SourceRejected(f"`{node.id}` is not allowed in a test")
        elif isinstance(node, ast.Attribute) and (
            node.attr in FORBIDDEN_ATTRIBUTES
            or node.attr.startswith("_")  # private and dunder attributes alike
            or (node.attr in EVALUATING_MARKS and id(node) not in safe_marks)
            or (node.attr in FORMAT_METHODS and id(node) not in safe_formats)
        ):
            raise SourceRejected(f"attribute `{node.attr}` is not allowed in a test")
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            raise SourceRejected(f"assigning to or deleting `.{node.attr}` is not allowed")
        elif isinstance(node, ast.Call) and _mark_name(node.func) in EVALUATING_MARKS:
            # `**{...}` has no keyword name; its value is a dict, which is not a constant either.
            conditions = [
                *node.args,
                *(k.value for k in node.keywords if k.arg in (None, "condition")),
            ]
            if any(not _is_plain_constant(condition) for condition in conditions):
                raise SourceRejected(
                    f"`{_mark_name(node.func)}` may only take a constant condition"
                )
            safe_marks.add(id(node.func))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in FORMAT_METHODS
        ):
            if not _is_string_literal(node.func.value):
                raise SourceRejected("`.format` may only be called on a single string literal")
            safe_formats.add(id(node.func))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _FORMAT_DUNDER.search(node.value):
                raise SourceRejected("a format string may not reach a name starting with `__`")
        elif isinstance(node, ast.MatchClass) and any(a.startswith("_") for a in node.kwd_attrs):
            raise SourceRejected("class patterns may not bind private attributes")
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
    # Pytest also collects `test_` methods of a `Test*` class and functions nested under an `if`,
    # so every `test_` function anywhere in the module carries the mark, not just top-level ones.
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef) and (
            function.name.startswith("test_")
            and not any(_marks_operation(d, operation_id) for d in function.decorator_list)
        ):
            raise SourceRejected(f"{function.name} lacks @pytest.mark.operation({operation_id!r})")


def _mark_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _is_string_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_plain_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and not isinstance(node.value, str)


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
