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

    def __init__(
        self,
        *,
        up_code: int = 0,
        port_out: str | None = None,
        down_code: int = 0,
        running: str = "",
        ps_code: int = 0,
        networks: str = "",
        net_ls_code: int = 0,
        net_rm_code: int = 0,
    ):
        self.networks = networks
        self.net_ls_code = net_ls_code
        self.net_rm_code = net_rm_code
        self.running = running
        self.ps_code = ps_code
        self.calls: list[tuple[list[str], str | None]] = []
        self.up_code = up_code
        self.port_out = port_out
        self.down_code = down_code
        self.ports = iter(range(50000, 50100))

    def __call__(self, argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        self.calls.append((list(argv), stdin))
        if argv[1] == "ps":
            return CommandResult(self.ps_code, self.running, "ps broke" if self.ps_code else "")
        if argv[1] == "network":
            if argv[2] == "ls":
                return CommandResult(
                    self.net_ls_code, self.networks, "ls broke" if self.net_ls_code else ""
                )
            return CommandResult(self.net_rm_code, "", "rm broke" if self.net_rm_code else "")
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
    argv, stdin = next(c for c in docker.calls if "up" in c[0])
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
    assert not (path.parent / "mocks").exists()  # a refused file creates nothing


def test_a_failed_start_is_cleaned_up(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(up_code=1)
    with pytest.raises(StackError, match="up failed: pull failed"):
        up("shop", path, text, docker)
    assert docker.verbs() == ["up", "down"]
    assert any("--volumes" in argv for argv, _ in docker.calls)


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
    argv, stdin = docker.calls[0]
    assert argv == [
        "docker", "compose", "-p", "hermetic-shop", "-f", "-",
        "down", "--volumes", "--remove-orphans",
    ]  # fmt: skip
    assert stdin == "name: hermetic-shop\nservices: {}\n"  # never a file from the cwd


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


# --- review fixes ---


def test_up_makes_the_mount_folders_before_docker_sees_them(stack: tuple[Path, str]) -> None:
    path, text = stack
    made: list[bool] = []

    def runner(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        if "up" in argv:
            made.append((path.parent / "mocks" / "payments").is_dir())
        return FakeDocker()(argv, stdin=stdin, timeout=timeout)

    up("shop", path, text, runner)
    assert made == [True]


def test_a_linked_mount_folder_is_refused(stack: tuple[Path, str], tmp_path: Path) -> None:
    path, text = stack
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (path.parent / "mocks").symlink_to(elsewhere)
    docker = FakeDocker()
    with pytest.raises(StackError, match="symlink"):
        up("shop", path, text, docker)
    assert docker.calls == []


def test_a_timeout_during_up_is_cleaned_up(stack: tuple[Path, str]) -> None:
    path, text = stack
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        calls.append(list(argv))
        if "up" in argv:
            raise StackError("docker did not finish within 300 seconds")
        return CommandResult(0, "", "")

    with pytest.raises(StackError, match="did not finish"):
        up("shop", path, text, runner)
    assert any("down" in argv for argv in calls)


def test_an_interrupt_during_up_is_cleaned_up(stack: tuple[Path, str]) -> None:
    path, text = stack
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CommandResult:
        calls.append(list(argv))
        if "up" in argv:
            raise KeyboardInterrupt
        return CommandResult(0, "", "")

    with pytest.raises(KeyboardInterrupt):
        up("shop", path, text, runner)
    assert any("down" in argv for argv in calls)


def test_run_command_turns_os_errors_into_stack_errors(tmp_path: Path) -> None:
    script = tmp_path / "docker"
    script.write_text("#!/bin/sh\n")  # not executable
    with pytest.raises(StackError, match="cannot run docker"):
        run_command([str(script)], stdin=None, timeout=5)


def test_run_command_survives_output_that_is_not_utf8(tmp_path: Path) -> None:
    script = tmp_path / "docker"
    script.write_text("#!/bin/sh\nprintf '\\377\\376 ok'\n")
    script.chmod(0o755)
    result = run_command([str(script)], stdin=None, timeout=5)
    assert result.stdout.endswith(" ok")


def test_an_existing_mount_folder_is_left_as_it_is(stack: tuple[Path, str]) -> None:
    path, text = stack
    kept = path.parent / "mocks" / "payments"
    kept.mkdir(parents=True)
    (kept / "stub.json").write_text("{}")
    up("shop", path, text, FakeDocker())
    assert (kept / "stub.json").read_text() == "{}"


# --- second round of review fixes ---


def test_port_lookups_get_the_checked_file_not_the_working_directory(
    stack: tuple[Path, str],
) -> None:
    path, text = stack
    docker = FakeDocker()
    up("shop", path, text, docker)
    lookups = [c for c in docker.calls if "port" in c[0]]
    assert lookups
    for argv, stdin in lookups:
        assert argv[argv.index("-f") + 1] == "-"
        assert stdin == text


def test_a_stack_that_is_already_running_is_left_alone(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(running="abc123\n")
    with pytest.raises(StackError, match="already running; run `hermetic down --service shop`"):
        up("shop", path, text, docker)
    assert docker.verbs() == []  # no up, and above all no down


def test_a_failing_running_check_stops_the_start(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(ps_code=1)
    with pytest.raises(StackError, match="cannot tell whether hermetic-shop is running: ps broke"):
        up("shop", path, text, docker)
    assert docker.verbs() == []


def test_a_failed_cleanup_says_what_to_run(stack: tuple[Path, str]) -> None:
    path, text = stack
    docker = FakeDocker(up_code=1, down_code=1)
    with pytest.raises(StackError) as caught:
        up("shop", path, text, docker)
    message = str(caught.value)
    assert "up failed: pull failed" in message
    assert "cleaning up also failed" in message
    assert "hermetic down --service shop" in message


def test_a_linked_folder_above_the_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    path = link / COMPOSE_NAME
    text = generate_compose(DEPS, real)
    docker = FakeDocker()
    with pytest.raises(StackError, match="symlink"):
        up("shop", path, text, docker)
    assert docker.calls == []
    assert not (real / "mocks").exists()


def test_up_command_refuses_a_linked_folder_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / COMPOSE_NAME).write_text(generate_compose(DEPS, real))
    (tmp_path / "link").symlink_to(real)
    result = cli_runner.invoke(
        app, ["up", "--service", "shop", "--file", str(tmp_path / "link" / COMPOSE_NAME)]
    )
    assert result.exit_code == 2
    assert "symlink" in result.output


def test_up_command_prints_errors_without_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def start(service: str, path: Path, text: str) -> list[Endpoint]:
        raise StackError("bad \x1b[2J path")

    monkeypatch.setattr(cli, "start_stack", start)
    file = tmp_path / COMPOSE_NAME
    file.write_text("x")
    result = cli_runner.invoke(app, ["up", "--service", "shop", "--file", str(file)])
    assert result.exit_code == 2
    assert "\x1b" not in result.output
    assert "bad" in result.output


def test_down_command_prints_errors_without_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stop(service: str) -> None:
        raise StackError("bad \x1b[2J path")

    monkeypatch.setattr(cli, "stop_stack", stop)
    result = cli_runner.invoke(app, ["down", "--service", "shop"])
    assert result.exit_code == 2
    assert "\x1b" not in result.output


def test_down_removes_the_networks_the_project_left() -> None:
    docker = FakeDocker(networks="n1\nn2\n")
    down("shop", docker)
    removed = [argv[3] for argv, _ in docker.calls if argv[1:3] == ["network", "rm"]]
    assert removed == ["n1", "n2"]
    listing = next(argv for argv, _ in docker.calls if argv[1:3] == ["network", "ls"])
    assert "label=com.docker.compose.project=hermetic-shop" in listing


def test_down_reports_a_network_it_cannot_list_or_remove() -> None:
    with pytest.raises(StackError, match="cannot list the networks of hermetic-shop: ls broke"):
        down("shop", FakeDocker(net_ls_code=1))
    with pytest.raises(StackError, match="cannot remove a network of hermetic-shop: rm broke"):
        down("shop", FakeDocker(networks="n1", net_rm_code=1))
