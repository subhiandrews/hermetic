from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic import cli
from hermetic.cli import app
from hermetic.discover import Dependency, DependencySet
from hermetic.provision import COMPOSE_NAME, Endpoint, StackError, down, generate_compose, up
from hermetic.provision.stack import CommandResult, run_command

cli_runner = CliRunner()


def dep(name: str, kind: str) -> Dependency:
    return Dependency.model_validate({"name": name, "kind": kind, "origin": "manual"})


DEPS = DependencySet(
    service="shop",
    prompt_version="1",
    docs_hash="h",
    dependencies=[dep("db-main", "postgres"), dep("payments", "http"), dep("cache", "redis")],
)


class FakeDocker:
    """Records each call; answers `port` with a port per call and the rest with `others`."""

    def __init__(self, *, up_code: int = 0, port_out: str | None = None, down_code: int = 0):
        self.calls: list[tuple[list[str], str | None]] = []
        self.up_code = up_code
        self.port_out = port_out
        self.down_code = down_code
        self.ports = iter(range(50000, 50100))

    def __call__(self, argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        self.calls.append((list(argv), stdin))
        if "up" in argv:
            return CommandResult(self.up_code, "", "pull failed" if self.up_code else "")
        if "port" in argv:
            out = self.port_out if self.port_out is not None else f"127.0.0.1:{next(self.ports)}"
            return CommandResult(0 if out else 1, out, "" if out else "no such service")
        return CommandResult(self.down_code, "", "daemon gone" if self.down_code else "")

    def verbs(self) -> list[str]:
        return [v for argv, _ in self.calls for v in ("up", "port", "down") if v in argv]


@pytest.fixture
def stack(tmp_path: Path) -> tuple[Path, str]:
    return tmp_path / COMPOSE_NAME, generate_compose(DEPS, tmp_path)


def test_up_starts_the_checked_text_and_reports_endpoints(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker()
    endpoints = up("shop", path, text, docker)
    argv, stdin = docker.calls[0]
    assert argv[:5] == ["docker", "compose", "-p", "hermetic-shop", "--project-directory"]
    assert argv[argv.index("-f") + 1] == "-"  # the file is read from stdin, not from disk
    assert stdin == text
    assert "--wait" in argv
    assert [(e.name, e.kind) for e in endpoints] == [
        ("db-main", "postgres"),
        ("payments", "http"),
        ("cache", "redis"),
    ]
    assert endpoints[0].url == "postgresql://hermetic:hermetic@127.0.0.1:50000/hermetic"
    assert endpoints[1].url == "http://127.0.0.1:50001"
    assert endpoints[2].variable == "HERMETIC_DEP_CACHE_URL"
    assert endpoints[0].variable == "HERMETIC_DEP_DB_MAIN_URL"
    assert docker.verbs() == ["up", "port", "port", "port"]


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        ("mysql", "mysql://root:hermetic@127.0.0.1:7/hermetic"),
        ("mongo", "mongodb://127.0.0.1:7"),
    ],
)
def test_urls_for_the_other_kinds(kind: str, url: str) -> None:
    assert Endpoint("x", kind, 7).url == url  # type: ignore[arg-type]


def test_a_refused_file_never_reaches_docker(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker()
    with pytest.raises(StackError, match="refusing to run"):
        up("shop", path, text.replace("no-new-privileges:true", "seccomp:unconfined"), docker)
    assert docker.calls == []


def test_a_stack_of_another_service_is_not_started(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker()
    with pytest.raises(StackError, match="not 'hermetic-other'"):
        up("other", path, text, docker)
    assert docker.calls == []


def test_a_failed_start_is_cleaned_up(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(up_code=1)
    with pytest.raises(StackError, match="up failed: pull failed"):
        up("shop", path, text, docker)
    assert docker.verbs() == ["up", "down"]
    assert "--volumes" in docker.calls[-1][0]


def test_an_unreadable_port_is_cleaned_up(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(port_out="")
    with pytest.raises(StackError, match="cannot find where db-main listens: no such service"):
        up("shop", path, text, docker)
    assert docker.verbs() == ["up", "port", "down"]


@pytest.mark.parametrize("answer", ["0.0.0.0:5000", "127.0.0.1:0", "127.0.0.1:70000", "junk"])
def test_only_loopback_ports_are_accepted(stack: tuple[Path, str], answer: str) -> None:
    path, text = stack
    docker = FakeDocker(port_out=answer)
    with pytest.raises(StackError, match=r"not published on 127\.0\.0\.1"):
        up("shop", path, text, docker)
    assert docker.verbs()[-1] == "down"


def test_a_failing_cleanup_does_not_hide_the_first_failure(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(up_code=1, down_code=1)
    with pytest.raises(StackError, match="up failed"):
        up("shop", path, text, docker)


def test_down_removes_the_project_and_its_data() -> None:
    docker = FakeDocker()
    down("shop", docker)
    assert docker.calls[0][0] == [
        "docker", "compose", "-p", "hermetic-shop", "down", "--volumes", "--remove-orphans",
    ]  # fmt: skip


def test_down_reports_a_failure() -> None:
    with pytest.raises(StackError, match="down failed: daemon gone"):
        down("shop", FakeDocker(down_code=1))


def test_docker_output_is_printed_without_control_characters(stack: tuple[Path, str]) -> None:
    path, text = stack

    def runner(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        return CommandResult(1, "", "\x1b[2Jboom") if "up" in argv else CommandResult(0, "", "")

    with pytest.raises(StackError) as caught:
        up("shop", path, text, runner)
    assert "\x1b" not in str(caught.value)
    assert "boom" in str(caught.value)


def test_silent_failures_still_say_something(stack: tuple[Path, str]) -> None:
    path, text = stack

    def runner(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        return CommandResult(3, "", "") if "up" in argv else CommandResult(0, "", "")

    with pytest.raises(StackError, match="exit code 3"):
        up("shop", path, text, runner)


def test_stdout_is_used_when_stderr_is_empty(stack: tuple[Path, str]) -> None:
    path, text = stack

    def runner(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        return CommandResult(1, "out only", "") if "up" in argv else CommandResult(0, "", "")

    with pytest.raises(StackError, match="out only"):
        up("shop", path, text, runner)


# --- the real command runner ---


def test_run_command_captures_output() -> None:
    result = run_command(["cat"], stdin="hello", timeout=10)
    assert result == CommandResult(0, "hello", "")


def test_run_command_without_docker() -> None:
    with pytest.raises(StackError, match="not installed"):
        run_command(["hermetic-no-such-binary"], stdin=None, timeout=10)


def test_run_command_times_out() -> None:
    with pytest.raises(StackError, match="did not finish"):
        run_command(["sleep", "5"], stdin=None, timeout=0.1)


def test_run_command_drops_compose_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSE_FILE", "/etc/elsewhere.yml")
    monkeypatch.setenv("HERMETIC_KEEP", "yes")
    result = run_command(["env"], stdin=None, timeout=10)
    assert "COMPOSE_FILE" not in result.stdout
    assert "HERMETIC_KEEP=yes" in result.stdout


def test_run_command_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_command(["docker", "ps; rm -rf /"], stdin=None, timeout=1)
    assert seen["argv"] == ["docker", "ps; rm -rf /"]
    assert "shell" not in seen


# --- the commands ---


def test_up_command_prints_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".hermetic" / "provision" / "shop" / COMPOSE_NAME
    target.parent.mkdir(parents=True)
    target.write_text(generate_compose(DEPS, target.parent))
    docker = FakeDocker()
    monkeypatch.setattr(
        cli, "start_stack", lambda service, path, text: up(service, path, text, docker)
    )
    result = cli_runner.invoke(app, ["up", "--service", "shop"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "HERMETIC_DEP_DB_MAIN_URL=postgresql://hermetic:hermetic@127.0.0.1:50000/hermetic",
        "HERMETIC_DEP_PAYMENTS_URL=http://127.0.0.1:50001",
        "HERMETIC_DEP_CACHE_URL=redis://127.0.0.1:50002",
    ]


def test_up_command_reports_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli_runner.invoke(app, ["up", "--service", "shop"])
    assert result.exit_code == 2
    assert "error:" in result.output


def test_up_command_reports_a_refused_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("name: hermetic-shop\nservices: {}\n")
    result = cli_runner.invoke(app, ["up", "--service", "shop", "--file", str(bad)])
    assert result.exit_code == 2
    assert "refusing to run" in result.output


def test_down_command(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(cli, "stop_stack", lambda service: down(service, docker))
    result = cli_runner.invoke(app, ["down", "--service", "shop"])
    assert result.exit_code == 0
    assert "stopped hermetic-shop" in result.output
    assert docker.verbs() == ["down"]


def test_down_command_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker(down_code=1)
    monkeypatch.setattr(cli, "stop_stack", lambda service: down(service, docker))
    result = cli_runner.invoke(app, ["down", "--service", "shop"])
    assert result.exit_code == 2
    result = cli_runner.invoke(app, ["down", "--service", "Bad Name"])
    assert result.exit_code == 2
