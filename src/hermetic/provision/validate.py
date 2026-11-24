"""The allowlist a compose file passes before anything runs it.

Provisioning talks to the host's Docker daemon, which is root-equivalent, so a compose file is
never trusted for being generated: this check runs on every one, ours and a user's. It is an
allowlist. A key it does not name is refused, so a construct nobody thought of is refused too.

The text is read the way Docker Compose reads it or not at all: a file that two YAML parsers
could read differently (anchors, merge keys, repeated keys, several documents) is refused, and
so is `$` anywhere, which Compose would replace from the environment *after* this check.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from hermetic.discover.models import MAX_DEPENDENCIES, NAME_PATTERN
from hermetic.provision.images import APPROVED_IMAGES

MAX_COMPOSE_BYTES = 262_144
ALLOWED_TOP_LEVEL = frozenset({"name", "services"})
ALLOWED_SERVICE_KEYS = frozenset(
    {
        "image",
        "command",
        "environment",
        "ports",
        "healthcheck",
        "tmpfs",
        "volumes",
        "security_opt",
        "restart",
        "depends_on",
        "mem_limit",
        "pids_limit",
    }
)
# The project is `hermetic-<service id>`, and a service id is at most 63 characters.
PROJECT_PREFIX = "hermetic-"
MAX_PROJECT_CHARS = len(PROJECT_PREFIX) + 63
MAX_NESTING = 32  # compose files nest a handful of levels; deeper is not one
MAX_MOUNT_ENTRIES = 10_000  # files and folders looked through under one mount
MAX_MEMORY_MB = 65_536
# Every pattern is applied with fullmatch: `$` alone would also accept a trailing newline.
_PROJECT = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{MAX_PROJECT_CHARS - 1}}}")
_SERVICE = re.compile(NAME_PATTERN)
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PORT = re.compile(r"127\.0\.0\.1:(\d{1,5})?:(\d{1,5})(?:/(?:tcp|udp))?")
_MEM = re.compile(r"([1-9]\d{0,5})([mg])")  # a limit of 0 means no limit to Docker
_TMPFS = re.compile(r"/[A-Za-z0-9_./-]*")
_HEALTH_KEYS = frozenset({"test", "interval", "timeout", "retries", "start_period"})
_DEPENDS_CONDITIONS = frozenset({"service_started", "service_healthy"})


def _printable(text: str) -> str:
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text
    )


class ComposeError(Exception):
    """A compose file that must not run. `problems` lists what is wrong with its content.

    A file that cannot be read as a compose file at all (too large, not YAML, not a mapping)
    gives the one reason it stopped on, since there is nothing further to check.
    """

    def __init__(self, problems: list[str]) -> None:
        # A problem can quote a key from the file, and a key can hold an escape sequence that
        # rewrites a terminal, so what is kept and printed is made printable here, once.
        self.problems = [_printable(problem) for problem in problems]
        super().__init__("; ".join(self.problems))


class _Loader(yaml.SafeLoader):
    """SafeLoader that refuses what two parsers could read differently."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.constructor.ConstructorError(
                None, None, "anchors and aliases are not allowed", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":  # the `<<` key
                raise yaml.constructor.ConstructorError(
                    None, None, "merge keys are not allowed", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=True)
            try:
                repeated = key in seen
            except TypeError:  # a list or mapping used as a key
                raise yaml.constructor.ConstructorError(
                    None, None, "a key must be a plain value", key_node.start_mark
                ) from None
            if repeated:
                raise yaml.constructor.ConstructorError(
                    None, None, f"the key {key!r} is repeated", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def check_compose(text: str, workdir: Path) -> dict[str, Any]:
    """The parsed file when it is allowed; ComposeError, naming every problem, when it is not.

    `workdir` is the directory a relative bind mount may reach into.
    """
    if len(text.encode("utf-8", errors="replace")) > MAX_COMPOSE_BYTES:
        raise ComposeError([f"the file is larger than {MAX_COMPOSE_BYTES} bytes"])
    try:
        document = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ComposeError([f"not valid YAML for a compose file: {_why(exc)}"]) from exc
    except ValueError as exc:  # a scalar that looks like a date but is not one (2001-02-30)
        raise ComposeError(["not valid YAML for a compose file: a value cannot be read"]) from exc
    except RecursionError as exc:  # brackets nested far deeper than the size cap would suggest
        raise ComposeError(["not valid YAML for a compose file: nested too deeply"]) from exc
    problems: list[str] = []
    mounts: dict[Path, str | None] = {}  # what was found under each mount source, looked at once
    if not isinstance(document, dict):
        raise ComposeError(["the file must be a mapping with a `services` key"])
    for key in document.keys() - ALLOWED_TOP_LEVEL:
        problems.append(f"{key}: not allowed at the top level (only name and services)")
    name = document.get("name")
    if not (isinstance(name, str) and _PROJECT.fullmatch(name) and name.startswith(PROJECT_PREFIX)):
        # Another project's name would make `up` and `down` act on its containers.
        problems.append(f"name: must be {PROJECT_PREFIX}<service>, a lowercase project name")
    services = document.get("services")
    if not isinstance(services, dict) or not services:
        problems.append("services: must list at least one service")
    elif len(services) > MAX_DEPENDENCIES:
        problems.append(f"services: more than {MAX_DEPENDENCIES}")
    else:
        for service, body in services.items():
            _check_service(service, body, set(services), workdir, problems, mounts)
    _find_interpolation(document, "", problems)
    if problems:
        raise ComposeError(sorted(set(problems)))
    return document


def _why(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError):
        return exc.problem or "invalid"
    return "invalid"  # pragma: no cover  (every error the loader raises carries a mark)


def _find_interpolation(node: object, where: str, problems: list[str], depth: int = 0) -> None:
    """Compose replaces `${...}` from the environment after this check ran, so none may stay."""
    if depth > MAX_NESTING:
        problems.append(f"{where or 'file'}: nested more than {MAX_NESTING} levels deep")
    elif isinstance(node, str):
        if "$" in node:
            problems.append(f"{where or 'file'}: `$` (variable interpolation) is not allowed")
    elif isinstance(node, dict):
        for key, value in node.items():
            path = f"{where}.{key}" if where else str(key)
            _find_interpolation(key, path, problems, depth + 1)
            _find_interpolation(value, path, problems, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _find_interpolation(item, where, problems, depth + 1)


def _check_service(
    service: object,
    body: object,
    names: set[Any],
    workdir: Path,
    problems: list[str],
    mounts: dict[Path, str | None],
) -> None:
    if not (isinstance(service, str) and _SERVICE.fullmatch(service)):
        problems.append(f"services.{service}: the service name must be a lowercase slug")
        return
    where = f"services.{service}"
    if not isinstance(body, dict):
        problems.append(f"{where}: must be a mapping")
        return
    for key in body.keys() - ALLOWED_SERVICE_KEYS:
        problems.append(f"{where}.{key}: not allowed")
    image = body.get("image")
    if not (isinstance(image, str) and image in APPROVED_IMAGES):
        problems.append(f"{where}.image: {image!r} is not an approved image")
    for key, check in (
        ("ports", _check_ports),
        ("tmpfs", _check_tmpfs),
        ("environment", _check_environment),
        ("healthcheck", _check_healthcheck),
        ("command", _check_command),
    ):
        if key in body:
            check(f"{where}.{key}", body[key], workdir, problems)
    if "volumes" in body:
        _check_volumes(f"{where}.volumes", body["volumes"], workdir, problems, mounts)
    _check_scalars(where, body, names, problems)


def _strings(value: object) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _check_ports(where: str, value: object, workdir: Path, problems: list[str]) -> None:
    entries = value if isinstance(value, list) else None
    if entries is None or not all(isinstance(e, str) for e in entries):
        problems.append(f"{where}: must be a list of 127.0.0.1:[host]:container strings")
        return
    for entry in entries:
        match = _PORT.fullmatch(entry)
        if not match or any(int(p) > 65535 for p in match.groups() if p):
            problems.append(f"{where}: {entry!r} must publish on 127.0.0.1 only, as a valid port")


def _check_volumes(
    where: str,
    value: object,
    workdir: Path,
    problems: list[str],
    mounts: dict[Path, str | None],
) -> None:
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        problems.append(f"{where}: must be a list of ./path:/container/path:ro strings")
        return
    root = workdir.resolve()
    for entry in value:
        parts = entry.split(":")
        if len(parts) != 3 or parts[2] != "ro":
            problems.append(f"{where}: {entry!r} must be ./source:/destination:ro")
            continue
        source, destination, _ = parts
        if not source.startswith("./") or not destination.startswith("/"):
            problems.append(f"{where}: {entry!r} must mount a ./relative path at an absolute path")
            continue
        if "\0" in source or ".." in PurePosixPath(source).parts:
            # Compose joins the path as text and Docker follows links, so a `..` can name two
            # different folders; and a NUL cannot be a path at all.
            problems.append(f"{where}: {entry!r} must not hold `..` or a NUL character")
            continue
        target = (root / source).resolve()
        if not target.is_relative_to(root):
            problems.append(f"{where}: {entry!r} reaches outside the working directory")
        elif target == root:
            problems.append(f"{where}: {entry!r} mounts the whole working directory")
        elif ".." in PurePosixPath(destination).parts:
            problems.append(f"{where}: {entry!r} has a destination with ..")
        else:
            if target not in mounts:
                try:
                    mounts[target] = _mount_problem(target)
                except OSError:  # something changed or cannot be read while it was looked at
                    mounts[target] = "cannot be checked"
            reason = mounts[target]
            if reason:
                problems.append(f"{where}: {entry!r} {reason}")


def _mount_problem(target: Path) -> str | None:
    """Why a mount source must be refused, or None. A read-only mount does not stop a container
    from connecting to a Unix socket, so a socket at or anywhere under the source (the Docker
    daemon's, an SSH agent's) is refused; so is a source too big to look through, and a folder
    that cannot be listed, since it may hold one."""
    if not target.exists():
        return None  # nothing there yet; the provisioner creates it and checks again first
    if stat.S_ISSOCK(target.lstat().st_mode):
        return "is a socket"
    if not target.is_dir():
        return None  # a plain file has nothing beneath it
    seen = 0
    folders = [target]
    while folders:
        with os.scandir(folders.pop()) as entries:  # lazily, so the cap stops a huge folder early
            for entry in entries:
                seen += 1
                if seen > MAX_MOUNT_ENTRIES:
                    return f"holds more than {MAX_MOUNT_ENTRIES} entries to look through"
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISSOCK(mode):
                    return f"holds a socket ({Path(entry.path).relative_to(target)})"
                if stat.S_ISDIR(mode):
                    folders.append(Path(entry.path))
    return None


def _check_tmpfs(where: str, value: object, workdir: Path, problems: list[str]) -> None:
    paths = _strings(value)
    if paths is None or not all(_TMPFS.fullmatch(p) for p in paths):
        problems.append(f"{where}: must be absolute container paths")


def _check_environment(where: str, value: object, workdir: Path, problems: list[str]) -> None:
    if isinstance(value, dict):
        pairs = [(k, v) for k, v in value.items()]
    elif isinstance(value, list) and all(isinstance(e, str) and "=" in e for e in value):
        pairs = [tuple(e.split("=", 1)) for e in value]
    else:
        problems.append(f"{where}: must be a mapping or a list of NAME=value strings")
        return
    for key, item in pairs:
        if not (isinstance(key, str) and _ENV_KEY.fullmatch(key)):
            problems.append(f"{where}: {key!r} is not a variable name")
        if not isinstance(item, str | int | float) or isinstance(item, bool):
            problems.append(f"{where}.{key}: the value must be text or a number")


def _check_healthcheck(where: str, value: object, workdir: Path, problems: list[str]) -> None:
    if not isinstance(value, dict) or value.keys() - _HEALTH_KEYS:
        problems.append(f"{where}: may hold only {', '.join(sorted(_HEALTH_KEYS))}")
        return
    test = value.get("test")
    if _strings(test) is None:
        problems.append(f"{where}.test: must be text or a list of text")


def _check_command(where: str, value: object, workdir: Path, problems: list[str]) -> None:
    ok = isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(e, str | int) for e in value)
    )
    if not ok:
        problems.append(f"{where}: must be text or a list")


def _check_scalars(where: str, body: dict[Any, Any], names: set[Any], problems: list[str]) -> None:
    if "restart" in body:
        restart = body["restart"]
        if not (restart == "no" or restart is False):  # YAML 1.1 reads an unquoted no as false
            problems.append(f"{where}.restart: only `no` is allowed")
    # The caps are required, not only checked when present: a file that leaves them out could
    # exhaust the host's memory or processes once it runs on the root-equivalent daemon.
    if not _memory_ok(body.get("mem_limit")):
        problems.append(f"{where}.mem_limit: required, a real limit such as 512m or 1g")
    limit = body.get("pids_limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 4096:
        problems.append(f"{where}.pids_limit: required, a number from 1 to 4096")
    if body.get("security_opt") != ["no-new-privileges:true"]:
        problems.append(f"{where}.security_opt: required, and only no-new-privileges:true")
    if "depends_on" in body:
        _check_depends_on(where, body["depends_on"], names, problems)


def _memory_ok(value: object) -> bool:
    """A limit from 1m up to the cap; 0 would mean no limit to Docker."""
    match = _MEM.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return False
    amount = int(match.group(1)) * (1024 if match.group(2) == "g" else 1)
    return amount <= MAX_MEMORY_MB


def _check_depends_on(where: str, value: object, names: set[Any], problems: list[str]) -> None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        targets = dict.fromkeys(value)
    elif isinstance(value, dict):
        targets = value
    else:
        problems.append(f"{where}.depends_on: must be a list of names or a mapping")
        return
    for target, detail in targets.items():
        if target not in names:
            problems.append(f"{where}.depends_on: {target!r} is not a service in this file")
        if detail is not None and not (
            isinstance(detail, dict)
            and set(detail) == {"condition"}
            and isinstance(detail["condition"], str)
            and detail["condition"] in _DEPENDS_CONDITIONS
        ):
            problems.append(f"{where}.depends_on.{target}: only a started or healthy condition")
