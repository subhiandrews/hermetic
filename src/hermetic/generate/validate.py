"""Deterministic checks on agent-written test source, before it is written to disk.

The suite runs in-process with the user's privileges, so these checks are a guardrail against
a confused or manipulated agent, not a sandbox: only a small set of imports is allowed, and
names, attributes and fixtures that reach the system are refused.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import string
import types

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
        "uuid",
    }
)  # not `typing`: on 3.14+ its public API (`evaluate_forward_ref`, ...) evals strings
FORBIDDEN_NAMES = frozenset(
    {
        "breakpoint", "compile", "delattr", "eval", "exec", "getattr", "globals", "help",
        "importorskip", "locals", "open", "setattr", "type", "usefixtures", "vars",
    }
)  # fmt: skip
# Attributes that reach the interpreter or the system from an allowed module (`json.codecs`).
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
        # The builtins' accessors, as attributes of anything that imported `builtins as bltns`.
        "bltns", "delattr", "getattr", "globals", "locals", "setattr", "vars",
    }
)  # fmt: skip
# pytest eval()s a string condition of `skipif` and `xfail` at collection, and a mark can be
# bound to a name, called twice or built from `**kwargs`, so they are refused outright: a test
# that needs to skip calls `pytest.skip(...)` in its body.
EVALUATING_MARKS = frozenset({"skipif", "xfail"})
FORMAT_METHODS = frozenset({"format", "format_map"})
# The only `pytest` attributes a test may use. The rest of its public API starts plugins,
# builds fixtures by hand or runs a nested session, so it is refused rather than listed.
PYTEST_API = frozenset({"approx", "fail", "mark", "param", "raises", "skip"})
# `pytest.mark.<anything>` is a valid mark, and `filterwarnings("ignore::pkg.Cls")` imports the
# module it names when the mark is applied, so only these may follow `pytest.mark`.
MARK_NAMES = frozenset({"operation", "parametrize", "skip"})
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
    safe_formats: set[int] = set()  # `.format` nodes called directly on a string literal
    modules: dict[str, str] = {}  # local name -> module, for `import json` and `import json as j`
    for found in ast.walk(tree):
        if isinstance(found, ast.Import):
            for alias in found.names:
                local = alias.asname or alias.name
                if modules.setdefault(local, alias.name) != alias.name:
                    # `import re as m` and `import json as m` would leave `m` meaning either
                    raise SourceRejected(f"`{local}` is bound to two different imports")
    attributed: set[int] = set()  # module names that are the base of an attribute access
    mark_bases: set[int] = set()  # `pytest.mark` nodes that are the base of `pytest.mark.<name>`
    for node in ast.walk(tree):  # parents come before their children, so `attributed` fills first
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base == "pytest" or base in modules:
                attributed.add(id(node.value))
            if base == "pytest" and node.attr not in PYTEST_API:
                allowed = ", ".join(sorted(PYTEST_API))
                raise SourceRejected(
                    f"attribute `{node.attr}` is not allowed in a test (from pytest: {allowed})"
                )
            if base == "pytest" and node.attr == "mark" and id(node) not in mark_bases:
                raise SourceRejected("`pytest.mark` may only be used as `pytest.mark.<name>`")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"
        ):
            mark_bases.add(id(node.value))
            if node.attr not in MARK_NAMES:
                allowed = ", ".join(sorted(MARK_NAMES))
                raise SourceRejected(
                    f"attribute `{node.attr}` is not allowed in a test (pytest.mark: {allowed})"
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
                    or (node.module == "pytest" and alias.name not in PYTEST_API - {"mark"})
                    or _is_module(node.module, alias.name)  # `from re import enum`
                ):
                    raise SourceRejected(f"importing `{alias.name}` is not allowed in a test")
        elif (bound := _bound_names(node)) and any(_reserved_name(n) for n in bound):
            # `def pytest_generate_tests` is a hook pytest calls from a test module, and
            # `import json as pytest_plugins` or `except E as __x` bind a name with no ast.Name
            raise SourceRejected(f"binding the name `{bound[0]}` is not allowed in a test")
        elif isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES
            or node.id in EVALUATING_MARKS
            or node.id == "usefixtures"
            or _reserved_name(node.id)  # `pytest_plugins` loads any plugin, `pytestmark` marks
            # `p = re` would hide `p.enum`: an imported module is only used as `module.name`
            or ((node.id == "pytest" or node.id in modules) and id(node) not in attributed)
        ):
            raise SourceRejected(f"`{node.id}` is not allowed in a test")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
            and _is_module(modules[node.value.id], node.attr)
        ):  # `json.tool` reaches argparse and `re.enum.bltns` the builtins: a way round every list
            raise SourceRejected(f"module `{node.attr}` may not be reached through another module")
        elif isinstance(node, ast.Attribute) and (
            node.attr in FORBIDDEN_ATTRIBUTES
            or node.attr.startswith("_")  # private and dunder attributes alike
            or node.attr in EVALUATING_MARKS
            or (node.attr in FORMAT_METHODS and id(node) not in safe_formats)
        ):
            raise SourceRejected(f"attribute `{node.attr}` is not allowed in a test")
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            raise SourceRejected(f"assigning to or deleting `.{node.attr}` is not allowed")
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


def _reserved_name(name: str) -> bool:
    return name.startswith(("__", "pytest_")) or name == "pytestmark"


def _bound_names(node: ast.AST) -> list[str]:
    """Names a statement or pattern binds without an `ast.Name` node."""
    names: list[str | None] = []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        names.append(node.name)
    elif isinstance(node, ast.alias):
        names.append(node.asname)
    elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        names.append(node.name)
    elif isinstance(node, ast.MatchMapping):
        names.append(node.rest)
    return [n for n in names if n]


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


def _check_module(name: str) -> None:
    if name not in ALLOWED_IMPORTS:  # exact names: `json.tool` is not `json`
        raise SourceRejected(f"importing `{name}` is not allowed in a test")


def _is_module(module: str, name: str) -> bool:
    """True if `module.name` is a module, not a function or class.

    That is a submodule (`json.tool`) or a module the allowed one imported (`re.enum`,
    `json.codecs`): a test reaches only what an allowed module defines, not what it imports,
    because what it imports leads to the builtins, the system and the file system. Only modules
    the test may import are looked at, so validation never imports a name the test merely mentions.
    """
    if module not in ALLOWED_IMPORTS:
        return False
    try:
        if importlib.util.find_spec(f"{module}.{name}") is not None:
            return True
    except (ImportError, ValueError, AttributeError):  # `uuid` is not a package, for one
        pass
    return isinstance(getattr(importlib.import_module(module), name, None), types.ModuleType)


def _marks_operation(decorator: ast.expr, operation_id: str) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == "pytest.mark.operation"
        and len(decorator.args) == 1
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == operation_id
    )
