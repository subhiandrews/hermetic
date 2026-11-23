"""Deterministic checks on agent-written test source, before it is written to disk.

The suite runs in-process with the user's privileges, so these checks are a guardrail against
a confused or manipulated agent, not a sandbox: only a small set of imports is allowed, and
names, attributes and fixtures that reach the system are refused.
"""

from __future__ import annotations

import ast
import importlib.util
import string

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
        # `pytest.raises(...) as e` gives `e.traceback[0].frame.eval(...)`, which evals with the
        # frame's real globals; `with_args` adds a string condition to a skipif/xfail mark.
        "eval", "exec", "frame", "traceback", "tb", "getrepr", "with_args",
    }
)  # fmt: skip
# `pytest.mark.skipif("...")` and `xfail("...")` eval() a string condition at collection.
# They are only allowed as a direct call whose arguments are all non-string constants.
EVALUATING_MARKS = frozenset({"skipif", "xfail"})
FORMAT_METHODS = frozenset({"format", "format_map"})
# The only `pytest` attributes a test may use. The rest of its public API starts plugins,
# builds fixtures by hand or runs a nested session, so it is refused rather than listed.
PYTEST_API = frozenset({"approx", "fail", "mark", "param", "raises", "skip"})
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
    modules = {  # local name -> module, for `import json` and `import json as j`
        (alias.asname or alias.name): alias.name
        for found in ast.walk(tree)
        if isinstance(found, ast.Import)
        for alias in found.names
    }
    attributed: set[int] = set()  # `pytest` names that are the base of an attribute access
    for node in ast.walk(tree):  # parents come before their children, so `attributed` fills first
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "pytest"
        ):
            attributed.add(id(node.value))
            if node.attr not in PYTEST_API:
                allowed = ", ".join(sorted(PYTEST_API))
                raise SourceRejected(
                    f"attribute `{node.attr}` is not allowed in a test (from pytest: {allowed})"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name)
                if alias.name == "pytest" and alias.asname:
                    raise SourceRejected("`pytest` may not be imported under another name")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                raise SourceRejected("relative imports are not allowed")
            _check_module(node.module)
            for alias in node.names:
                if (
                    alias.name == "*"
                    or alias.name.startswith("_")  # private helpers: `uuid._get_command_stdout`
                    or alias.name in FORBIDDEN_NAMES | FORBIDDEN_ATTRIBUTES | EVALUATING_MARKS
                    or (node.module == "pytest" and alias.name not in PYTEST_API)
                    or _is_submodule(node.module, alias.name)  # `from json import tool`
                ):
                    raise SourceRejected(f"importing `{alias.name}` is not allowed in a test")
        elif isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES
            or node.id in EVALUATING_MARKS
            or node.id == "usefixtures"
            or node.id.startswith(("__", "pytest_"))  # `pytest_plugins` loads any plugin
            or (node.id == "pytest" and id(node) not in attributed)  # `p = pytest` hides the API
        ):
            raise SourceRejected(f"`{node.id}` is not allowed in a test")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
            and _is_submodule(modules[node.value.id], node.attr)
        ):  # `json.tool` reaches argparse, and so any file
            raise SourceRejected(f"submodule `{node.attr}` is not allowed in a test")
        elif isinstance(node, ast.Attribute) and (
            node.attr in FORBIDDEN_ATTRIBUTES
            or node.attr.startswith("_")  # private and dunder attributes alike
            or (node.attr in EVALUATING_MARKS and id(node) not in safe_marks)
            or (node.attr in FORMAT_METHODS and id(node) not in safe_formats)
        ):
            raise SourceRejected(f"attribute `{node.attr}` is not allowed in a test")
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            raise SourceRejected(f"assigning to or deleting `.{node.attr}` is not allowed")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Call)
            and _mark_name(node.func.func) in EVALUATING_MARKS
        ):
            # `skipif(False)("os.system('id')")` adds a string condition after the checked call.
            raise SourceRejected("a skipif/xfail mark may not be called a second time")
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
            receiver = node.func.value
            if not (isinstance(receiver, ast.Constant) and isinstance(receiver.value, str)):
                raise SourceRejected("`.format` may only be called on a single string literal")
            if _has_field_access(receiver.value):
                raise SourceRejected("a format field may not use attribute or index access")
            safe_formats.add(id(node.func))
        elif isinstance(node, ast.MatchClass) and any(
            a.startswith("_") or a in FORBIDDEN_ATTRIBUTES | FORMAT_METHODS | EVALUATING_MARKS
            for a in node.kwd_attrs
        ):  # `case object(sys=s)` reads `.sys` without an ast.Attribute node
            raise SourceRejected("class patterns may not bind forbidden or private attributes")
        elif isinstance(node, ast.arg) and node.arg in FORBIDDEN_FIXTURES:
            raise SourceRejected(f"fixture `{node.arg}` is not allowed in a test")
        elif isinstance(node, ast.keyword) and (node.arg or "").startswith("_"):
            raise SourceRejected(f"keyword argument `{node.arg}` is not allowed in a test")
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


def _has_field_access(template: str) -> bool:
    """True if a replacement field, or a field nested in a format spec, uses `.` or `[`.

    `"{0.sys.modules[os]}".format(typing)` reads attributes by name, outside any `ast.Attribute`
    node, so the fields themselves are parsed.
    """
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError:
        return False  # not a valid format string, so `.format` raises on it anyway
    for _, name, spec, _ in fields:
        if name and ("." in name or "[" in name):
            return True
        if spec and _has_field_access(spec):
            return True
    return False


def _is_plain_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and not isinstance(node.value, str)


def _check_module(name: str) -> None:
    if name not in ALLOWED_IMPORTS:  # exact names: `json.tool` is not `json`
        raise SourceRejected(f"importing `{name}` is not allowed in a test")


def _is_submodule(module: str, name: str) -> bool:
    """True if `module.name` is a module itself, not an attribute: `json.tool`, not `json.loads`."""
    try:
        return importlib.util.find_spec(f"{module}.{name}") is not None
    except (ImportError, ValueError, AttributeError):  # `uuid` is not a package, for one
        return False


def _marks_operation(decorator: ast.expr, operation_id: str) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == "pytest.mark.operation"
        and len(decorator.args) == 1
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == operation_id
    )
