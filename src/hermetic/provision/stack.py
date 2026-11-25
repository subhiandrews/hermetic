"""Start and stop a compose stack, and say where its engines can be reached.

Only a file that passed the allowlist is run, and the text that passed is the text handed to
Docker (on standard input), so the file cannot change between the check and the start. Docker is
reached through `CommandRunner`, so tests use a fake and never need a daemon.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hermetic.discover.models import DependencyKind
from hermetic.provision.compose import ProvisionError, refuse_links
from hermetic.provision.images import ENGINES
from hermetic.provision.validate import PROJECT_PREFIX, ComposeError, check_compose, printable

UP_TIMEOUT_SECONDS = 300.0  # image pulls included
COMMAND_TIMEOUT_SECONDS = 60.0
WAIT_TIMEOUT_SECONDS = 180  # how long Compose waits for the health checks
_ENDPOINT = re.compile(r"^127\.0\.0\.1:(\d{1,5})$")
_MAX_OUTPUT_CHARS = 2000
_MAX_ENGINE_LOGS = 5  # engines whose logs are added to a failed start's error
_URLS: dict[DependencyKind, str] = {
    "postgres": "postgresql://hermetic:hermetic@127.0.0.1:{port}/hermetic",
    "mysql": "mysql://root:hermetic@127.0.0.1:{port}/hermetic",
    "redis": "redis://127.0.0.1:{port}",
    "mongo": "mongodb://127.0.0.1:{port}",
    "http": "http://127.0.0.1:{port}",
}
_KIND_OF_IMAGE: dict[str, DependencyKind] = {e.image: kind for kind, e in ENGINES.items()}


class StackError(Exception):
    """The stack could not be started, stopped or described."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> CommandResult: ...


def run_command(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
    """Run a fixed argument list, never through a shell, with Compose's own settings removed."""
    # COMPOSE_FILE, COMPOSE_PROJECT_NAME and the like would let the environment steer which file
    # or project the command acts on.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    try:
        done = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StackError("docker is not installed or not on PATH") from exc
    except OSError as exc:  # not executable, and the like
        raise StackError(f"cannot run docker: {exc.strerror or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StackError(f"docker did not finish within {timeout:.0f} seconds") from exc
    return CommandResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Endpoint:
    name: str  # the compose service, which is the dependency's name
    kind: DependencyKind
    port: int  # on 127.0.0.1

    @property
    def url(self) -> str:
        return _URLS[self.kind].format(port=self.port)

    @property
    def variable(self) -> str:
        return f"HERMETIC_DEP_{self.name.upper().replace('-', '_')}_URL"


def _say(result: CommandResult) -> str:
    text = (result.stderr or result.stdout).strip()
    # the end, not the start: Docker prints pull progress first and what went wrong last
    return printable(text[-_MAX_OUTPUT_CHARS:]) or f"exit code {result.returncode}"


def _compose(
    runner: CommandRunner, project: str, args: Sequence[str], *, text: str, timeout: float
) -> CommandResult:
    # Always `-f -`: without a file Compose would load whatever sits in the working directory,
    # which never passed the allowlist.
    argv = ["docker", "compose", "-p", project, "-f", "-", *args]
    return runner(argv, stdin=text, timeout=timeout)


def _stub(project: str) -> str:
    """A file with no services: enough for Compose to find a project's containers by name."""
    return f"name: {project}\nservices: {{}}\n"


def project_name(service: str) -> str:
    return f"{PROJECT_PREFIX}{service}"


def up(service: str, path: Path, text: str, runner: CommandRunner = run_command) -> list[Endpoint]:
    """Check `text` (the content of the file at `path`), start it, and say where it listens.

    The file must be the stack of `service`: `down` and the next `up` find it by that name.

    On any failure after the start has begun, what was started is taken down again.
    """
    try:
        refuse_links(path)
        document = check_compose(text, path.parent)
        project = str(document["name"])
        if project != project_name(service):  # before anything is created on disk
            raise StackError(f"{path} is the stack {project!r}, not {project_name(service)!r}")
        # Compose reads a `.env` beside the file, where COMPOSE_* settings could steer it again.
        if (path.parent / ".env").exists():
            raise StackError(
                f"{path.parent / '.env'} exists; Compose would read it, so not starting"
            )
        _make_mount_sources(path.parent, document)
        # A source that did not exist was accepted above; now every one exists, so look again.
        document = check_compose(text, path.parent)
    except ProvisionError as exc:
        raise StackError(str(exc)) from exc
    except ComposeError as exc:
        raise StackError(f"refusing to run {path}: {exc}") from exc
    _refuse_if_running(runner, project, service)
    argv = [
        "docker",
        "compose",
        "-p",
        project,
        "--project-directory",
        str(path.parent.resolve()),
        "-f",
        "-",
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(WAIT_TIMEOUT_SECONDS),
    ]
    try:
        started = runner(argv, stdin=text, timeout=UP_TIMEOUT_SECONDS)
        if started.returncode != 0:
            raise StackError(f"docker compose up failed: {_say(started)}")
        return _endpoints(runner, project, text, document["services"])
    except BaseException as failure:  # a timeout or Ctrl-C can leave containers behind too
        # read what the engines said before taking them down: it is the only place the reason is
        engines = _exited_engines(runner, project) if isinstance(failure, StackError) else ""
        left = _take_down(runner, project)
        hint = ""
        if left is not None:
            hint = f"cleaning up also failed ({left}); run `hermetic down --service {service}`"
        if isinstance(failure, StackError):
            message = f"{failure}{engines}" + (f"; {hint}" if hint else "")
            if engines or hint:
                raise StackError(message) from failure
        elif hint:
            failure.add_note(hint)  # an interrupt or other error keeps its own type
        raise


def _exited_engines(runner: CommandRunner, project: str) -> str:
    """The last lines of each engine that exited, for the error: Compose only says "exited (1)"."""
    try:
        listed = runner(
            [
                "docker",
                "ps",
                "--all",
                "--format",
                "{{.Names}}",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "status=exited",
            ],
            stdin=None,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        out = []
        for name in listed.stdout.split()[:_MAX_ENGINE_LOGS] if listed.returncode == 0 else []:
            logs = runner(
                ["docker", "logs", "--tail", "15", name],
                stdin=None,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            text = (logs.stdout + logs.stderr).strip()
            if text:
                out.append(f"\n--- {printable(name)} said:\n{printable(text[-_MAX_OUTPUT_CHARS:])}")
        return "".join(out)
    except StackError:  # the reason for the first failure is still worth reporting
        return ""


def _refuse_if_running(runner: CommandRunner, project: str, service: str) -> None:
    """A second start would, on failing, take down the first one's stack with it."""
    listed = runner(
        ["docker", "ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}"],
        stdin=None,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        raise StackError(f"cannot tell whether {project} is running: {_say(listed)}")
    if listed.stdout.strip():
        raise StackError(f"{project} is already running; run `hermetic down --service {service}`")


def _make_mount_sources(folder: Path, document: Mapping[str, Mapping[str, object]]) -> None:
    """Create the folders a service mounts, so Docker never creates one itself.

    Docker makes a missing source at bind time, and by then it could be a link to anywhere, so
    each folder is made here, one step at a time, refusing a link at any step.
    """
    services = document["services"]
    assert isinstance(services, Mapping)  # check_compose has said so
    for service in services.values():
        for volume in service.get("volumes", []):  # type: ignore[attr-defined]
            source = str(volume).split(":", 1)[0]
            step = folder
            for part in Path(source).parts:
                step = step / part
                if step.is_symlink():
                    raise StackError(f"{step} is a symlink; not mounting through it")
                if not step.exists():
                    step.mkdir()


def _endpoints(
    runner: CommandRunner, project: str, text: str, services: Mapping[str, Mapping[str, object]]
) -> list[Endpoint]:
    found: list[Endpoint] = []
    for name, service in services.items():
        kind = _KIND_OF_IMAGE[str(service["image"])]
        asked = _compose(
            runner,
            project,
            ["port", name, str(ENGINES[kind].port)],
            text=text,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        if asked.returncode != 0:
            raise StackError(f"cannot find where {name} listens: {_say(asked)}")
        match = _ENDPOINT.match(asked.stdout.strip())
        port = int(match.group(1)) if match else 0
        if not 0 < port < 65536:
            raise StackError(f"{name} is not published on 127.0.0.1 as expected")
        found.append(Endpoint(name, kind, port))
    return found


def _take_down(runner: CommandRunner, project: str) -> str | None:
    """Try to remove the project; the reason it could not be, or None."""
    try:
        down_project(runner, project)
    except StackError as exc:
        return str(exc)
    return None


def down_project(runner: CommandRunner, project: str) -> None:
    done = _compose(
        runner,
        project,
        ["down", "--volumes", "--remove-orphans"],
        text=_stub(project),
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if done.returncode != 0:
        raise StackError(f"docker compose down failed: {_say(done)}")
    # With no services in the stub, Compose leaves the project's network behind.
    label = f"label=com.docker.compose.project={project}"
    listed = runner(
        ["docker", "network", "ls", "--quiet", "--filter", label],
        stdin=None,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        raise StackError(f"cannot list the networks of {project}: {_say(listed)}")
    for network in listed.stdout.split():
        removed = runner(
            ["docker", "network", "rm", network], stdin=None, timeout=COMMAND_TIMEOUT_SECONDS
        )
        if removed.returncode != 0:
            raise StackError(f"cannot remove a network of {project}: {_say(removed)}")


def down(service: str, runner: CommandRunner = run_command) -> None:
    """Stop and remove what `up` started for the service, data included."""
    down_project(runner, project_name(service))
