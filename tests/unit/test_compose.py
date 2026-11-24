from __future__ import annotations

import copy
import os
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.discover import Dependency, DependencySet, write_dependencies
from hermetic.discover.models import MAX_DEPENDENCIES
from hermetic.provision import (
    APPROVED_IMAGES,
    COMPOSE_NAME,
    ENGINES,
    ComposeError,
    ProvisionError,
    check_compose,
    generate_compose,
    read_compose_file,
    write_compose,
)
from hermetic.provision.validate import (
    MAX_COMPOSE_BYTES,
    MAX_MEMORY_MB,
    MAX_NESTING,
    MAX_PROJECT_CHARS,
    PROJECT_PREFIX,
)

GOLDEN = Path(__file__).parent.parent / "fixtures" / "compose" / "all-kinds.yml"
runner = CliRunner()


def dep(name: str, kind: str) -> Dependency:
    return Dependency.model_validate({"name": name, "kind": kind, "origin": "manual"})


def deps_set(*dependencies: Dependency, service: str = "shop") -> DependencySet:
    return DependencySet(
        service=service, prompt_version="1", docs_hash="h", dependencies=list(dependencies)
    )


ALL_KINDS = deps_set(
    dep("postgres", "postgres"),
    dep("mysql", "mysql"),
    dep("redis", "redis"),
    dep("mongo", "mongo"),
    dep("payments", "http"),
)


# --- generation ---


def test_every_kind_comes_out_as_the_reviewed_file(tmp_path: Path) -> None:
    assert generate_compose(ALL_KINDS, tmp_path) == GOLDEN.read_text()


def test_the_same_set_gives_the_same_text(tmp_path: Path) -> None:
    assert generate_compose(ALL_KINDS, tmp_path) == generate_compose(ALL_KINDS, tmp_path)


@pytest.mark.parametrize("kind", sorted(ENGINES))
def test_each_kind_alone_is_a_file_the_allowlist_accepts(tmp_path: Path, kind: str) -> None:
    text = generate_compose(deps_set(dep("thing", kind)), tmp_path)
    service = check_compose(text, tmp_path)["services"]["thing"]
    assert service["image"] == ENGINES[kind].image  # type: ignore[index]
    assert service["ports"] == [f"127.0.0.1::{ENGINES[kind].port}"]  # type: ignore[index]


def test_everything_generated_is_local_and_unprivileged(tmp_path: Path) -> None:
    document = yaml.safe_load(generate_compose(ALL_KINDS, tmp_path))
    for service in document["services"].values():
        assert service["ports"][0].startswith("127.0.0.1:")
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["image"] in APPROVED_IMAGES
        assert "privileged" not in service and "volumes_from" not in service
    assert document["name"] == "hermetic-shop"


def test_nothing_to_provision_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProvisionError, match="no dependencies"):
        generate_compose(deps_set(), tmp_path)


def test_a_compose_file_is_written_whole_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out" / COMPOSE_NAME
    write_compose(target, "first\n")
    assert target.read_text() == "first\n" and os.stat(target).st_mode & 0o777 == 0o644

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hermetic.provision.compose.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_compose(target, "second\n")
    assert target.read_text() == "first\n"
    assert [p.name for p in target.parent.iterdir()] == [COMPOSE_NAME]


def test_reading_a_compose_file_is_capped_and_never_through_a_link(tmp_path: Path) -> None:
    good = tmp_path / "a.yml"
    good.write_text("services: {}\n")
    assert read_compose_file(good) == "services: {}\n"
    big = tmp_path / "big.yml"
    big.write_text(" " * (MAX_COMPOSE_BYTES + 1))
    with pytest.raises(ComposeError, match="larger than"):
        read_compose_file(big)
    bad = tmp_path / "bad.yml"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(ComposeError, match="UTF-8"):
        read_compose_file(bad)
    link = tmp_path / "link.yml"
    link.symlink_to(good)
    with pytest.raises(OSError):
        read_compose_file(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_is_not_read_as_a_compose_file(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.yml"
    os.mkfifo(fifo)
    with pytest.raises(ComposeError, match="not a regular file"):
        read_compose_file(fifo)


# --- the allowlist ---


def good_service(**changes: Any) -> dict[str, Any]:
    service: dict[str, Any] = {
        "image": "redis:7-alpine",
        "ports": ["127.0.0.1::6379"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "no",
    }
    service.update(changes)
    return service


def document_with(**changes: Any) -> str:
    return yaml.safe_dump({"name": "hermetic-x", "services": {"db": good_service(**changes)}})


def problems_of(text: str, workdir: Path) -> list[str]:
    with pytest.raises(ComposeError) as raised:
        check_compose(text, workdir)
    assert str(raised.value) == "; ".join(raised.value.problems)
    return raised.value.problems


def test_a_plain_good_file_is_accepted(tmp_path: Path) -> None:
    assert check_compose(document_with(), tmp_path)["name"] == "hermetic-x"


def test_the_forms_compose_accepts_for_the_allowed_keys(tmp_path: Path) -> None:
    (tmp_path / "stubs").mkdir()
    text = """
name: hermetic-x
services:
  db:
    image: postgres:16-alpine
    environment:
      - POSTGRES_PASSWORD=hermetic
      - NUMBER=5
    tmpfs: /var/lib/postgresql/data
    healthcheck:
      test: pg_isready
      retries: 3
    restart: no
    mem_limit: 1g
    pids_limit: 100
    ports: ["127.0.0.1:5432:5432", "127.0.0.1::5433/tcp"]
    command: [serve, 80]
  api:
    image: wiremock/wiremock:3.9.1
    volumes: ["./stubs:/home/wiremock/mappings:ro"]
    depends_on:
      db:
        condition: service_healthy
  other:
    image: redis:7-alpine
    command: redis-server
    depends_on: [db, api]
"""
    assert set(check_compose(text, tmp_path)["services"]) == {"db", "api", "other"}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"privileged": True}, "services.db.privileged: not allowed"),
        ({"network_mode": "host"}, "network_mode: not allowed"),
        ({"pid": "host"}, "pid: not allowed"),
        ({"ipc": "host"}, "ipc: not allowed"),
        ({"cap_add": ["SYS_ADMIN"]}, "cap_add: not allowed"),
        ({"devices": ["/dev/sda:/dev/sda"]}, "devices: not allowed"),
        ({"build": "."}, "build: not allowed"),
        ({"extends": {"file": "x.yml", "service": "y"}}, "extends: not allowed"),
        ({"env_file": ".env"}, "env_file: not allowed"),
        ({"user": "root"}, "user: not allowed"),
        ({"entrypoint": ["sh"]}, "entrypoint: not allowed"),
        ({"container_name": "x"}, "container_name: not allowed"),
        ({"userns_mode": "host"}, "userns_mode: not allowed"),
        ({"image": "ubuntu:22.04"}, "not an approved image"),
        ({"image": "redis:latest"}, "not an approved image"),
        ({"image": ["redis:7-alpine"]}, "not an approved image"),
        ({"image": None}, "not an approved image"),
        ({"ports": ["0.0.0.0:6379:6379"]}, "127.0.0.1 only"),
        ({"ports": ["6379:6379"]}, "127.0.0.1 only"),
        ({"ports": ["127.0.0.1:6379:99999"]}, "valid port"),
        ({"ports": [6379]}, "list of 127.0.0.1"),
        ({"ports": [{"target": 6379, "published": 6379}]}, "list of 127.0.0.1"),
        ({"ports": "127.0.0.1::6379"}, "list of 127.0.0.1"),
        ({"restart": "always"}, "only `no` is allowed"),
        ({"restart": 0}, "only `no` is allowed"),
        ({"ports": ["127.0.0.1::6379\n"]}, "127.0.0.1 only"),
        ({"mem_limit": "512m\n"}, "real limit"),
        ({"mem_limit": "0m"}, "real limit"),
        ({"mem_limit": "0g"}, "real limit"),
        ({"mem_limit": "00512m"}, "real limit"),
        ({"mem_limit": "65537m"}, "real limit"),
        ({"mem_limit": "65g"}, "real limit"),
        ({"tmpfs": ["/data\n"]}, "absolute container paths"),
        ({"environment": {"A\n": "x"}}, "not a variable name"),
        ({"depends_on": {"db": {"condition": ["service_healthy"]}}}, "condition"),
        ({"depends_on": {"db": {"condition": {"a": 1}}}}, "condition"),
        ({"mem_limit": "lots"}, "real limit"),
        ({"mem_limit": 512}, "real limit"),
        ({"pids_limit": 0}, "pids_limit"),
        ({"pids_limit": True}, "pids_limit"),
        ({"pids_limit": "many"}, "pids_limit"),
        ({"security_opt": ["seccomp=unconfined"]}, "security_opt"),
        ({"security_opt": []}, "security_opt"),
        ({"tmpfs": ["relative"]}, "absolute container paths"),
        ({"tmpfs": 5}, "absolute container paths"),
        ({"environment": "A=1"}, "mapping or a list"),
        ({"environment": ["NOEQUALS"]}, "mapping or a list"),
        ({"environment": {"1BAD": "x"}}, "not a variable name"),
        ({"environment": {"A": ["x"]}}, "text or a number"),
        ({"environment": {"A": True}}, "text or a number"),
        ({"healthcheck": {"test": "x", "disable": True}}, "may hold only"),
        ({"healthcheck": "curl x"}, "may hold only"),
        ({"healthcheck": {"test": 5}}, "healthcheck.test"),
        ({"command": {"a": "b"}}, "must be text or a list"),
        ({"command": [["nested"]]}, "must be text or a list"),
        ({"depends_on": ["ghost"]}, "not a service in this file"),
        ({"depends_on": 5}, "list of names or a mapping"),
        ({"depends_on": [["db"]]}, "list of names or a mapping"),
        ({"depends_on": {"db": {"condition": "service_completed_successfully"}}}, "condition"),
        ({"depends_on": {"db": "yes"}}, "condition"),
        ({"volumes": "./a:/b:ro"}, "list of ./path"),
        ({"volumes": [5]}, "list of ./path"),
    ],
)
def test_a_construct_outside_the_allowlist_is_refused(
    tmp_path: Path, changes: dict[str, Any], expected: str
) -> None:
    assert any(expected in problem for problem in problems_of(document_with(**changes), tmp_path))


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        ("/var/run/docker.sock:/var/run/docker.sock:ro", "./relative path"),
        ("/etc:/host:ro", "./relative path"),
        ("../outside:/x:ro", "./relative path"),
        ("./../outside:/x:ro", "outside the working directory"),
        ("./stubs/../../outside:/x:ro", "outside the working directory"),
        ("./stubs:/x:rw", "./source:/destination:ro"),
        ("./stubs:/x", "./source:/destination:ro"),
        ("named-volume:/x:ro", "./relative path"),
        ("./stubs:relative:ro", "./relative path"),
        ("./stubs:/a/../b:ro", "destination with .."),
        ("./link:/x:ro", "outside the working directory"),
        ("./s.sock:/x:ro", "is a socket"),
        ("./:/x:ro", "whole working directory"),
        ("./.:/x:ro", "whole working directory"),
        ("./sockdir:/x:ro", "holds a socket (inner/deeper.sock)"),
    ],
)
def test_a_mount_may_only_reach_read_only_into_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, volume: str, expected: str
) -> None:
    work = tmp_path / "work"
    (work / "stubs").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (work / "link").symlink_to(tmp_path / "outside")
    (work / "sockdir" / "inner").mkdir(parents=True)
    monkeypatch.chdir(work)
    listeners = [socket.socket(socket.AF_UNIX), socket.socket(socket.AF_UNIX)]
    listeners[0].bind("s.sock")
    listeners[1].bind("sockdir/inner/deeper.sock")
    try:
        problems = problems_of(document_with(volumes=[volume]), work)
    finally:
        for listener in listeners:
            listener.close()
    assert any(expected in problem for problem in problems)


def test_a_mount_with_too_many_files_to_look_through_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "big").mkdir()
    for i in range(5):
        (tmp_path / "big" / f"f{i}").write_text("x")
    monkeypatch.setattr("hermetic.provision.validate.MAX_MOUNT_ENTRIES", 3)
    problems = problems_of(document_with(volumes=["./big:/m:ro"]), tmp_path)
    assert any("more than 3 entries" in p for p in problems)


def test_a_mount_that_cannot_be_looked_through_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(target: Path) -> str | None:
        raise PermissionError("denied")

    monkeypatch.setattr("hermetic.provision.validate._mount_problem", deny)
    problems = problems_of(document_with(volumes=["./x:/m:ro"]), tmp_path)
    assert any("cannot be checked" in p for p in problems)


def test_a_mount_of_a_plain_file_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "one.json").write_text("{}")
    assert check_compose(document_with(volumes=["./one.json:/m:ro"]), tmp_path)


def test_a_mount_of_a_folder_inside_the_working_directory_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "stubs").mkdir()
    assert check_compose(document_with(volumes=["./stubs:/m:ro"]), tmp_path)


@pytest.mark.parametrize("limit", ["1m", "512m", "1g", f"{MAX_MEMORY_MB}m", "64g"])
def test_a_real_memory_limit_is_accepted(tmp_path: Path, limit: str) -> None:
    assert check_compose(document_with(mem_limit=limit), tmp_path)


def test_a_mount_of_something_not_yet_created_is_accepted(tmp_path: Path) -> None:
    assert check_compose(document_with(volumes=["./mocks/x:/m:ro"]), tmp_path)


@pytest.mark.parametrize(
    ("changes", "where"),
    [
        ({"image": "${IMAGE}"}, "image"),
        ({"environment": {"A": "${SECRET}"}}, "environment.A"),
        ({"command": ["echo", "$HOME"]}, "command"),
    ],
)
def test_variable_interpolation_is_refused_because_compose_runs_it_after_the_check(
    tmp_path: Path, changes: dict[str, Any], where: str
) -> None:
    assert any("`$`" in p and where in p for p in problems_of(document_with(**changes), tmp_path))


def test_interpolation_in_a_key_is_refused_too(tmp_path: Path) -> None:
    text = document_with(environment={"A$B": "x"})
    assert any("`$`" in p for p in problems_of(text, tmp_path))


@pytest.mark.parametrize("top", ["volumes", "networks", "secrets", "configs", "include", "version"])
def test_nothing_but_name_and_services_is_allowed_at_the_top_level(
    tmp_path: Path, top: str
) -> None:
    text = document_with() + f"{top}: {{}}\n"
    assert any(f"{top}: not allowed at the top level" in p for p in problems_of(text, tmp_path))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("services: &a\n  db: {image: 'redis:7-alpine'}\nname: *a\n", "anchors and aliases"),
        ("services:\n  db: &x {image: 'redis:7-alpine'}\n", "anchors and aliases"),
        (
            "services:\n  db:\n    <<: {privileged: true}\n    image: 'redis:7-alpine'\n",
            "merge keys",
        ),
        (
            "services:\n  db:\n    image: 'redis:7-alpine'\n    image: 'redis:7-alpine'\n",
            "repeated",
        ),
        ("services:\n  db: {image: 'redis:7-alpine'}\n---\nservices: {}\n", "not valid YAML"),
        ("services:\n  ? [a, b]\n  : c\n", "plain value"),
        ("services: [unclosed\n", "not valid YAML"),
        ("!!python/object/apply:os.system [x]\n", "not valid YAML"),
        ("services:\n  db: {image: 'redis:7-alpine'}\nwhen: 2001-02-30\n", "cannot be read"),
        ("[" * 100_000, "nested too deeply"),
    ],
)
def test_a_file_two_parsers_could_read_differently_is_refused(
    tmp_path: Path, text: str, expected: str
) -> None:
    assert any(expected in problem for problem in problems_of(text, tmp_path))


def test_a_document_nested_past_the_limit_is_refused_even_when_it_parses(tmp_path: Path) -> None:
    nested: Any = "leaf"
    for _ in range(MAX_NESTING + 8):
        nested = {"k": nested}
    text = yaml.safe_dump({"services": {"db": good_service()}, "extra": nested})
    assert any(f"nested more than {MAX_NESTING} levels" in p for p in problems_of(text, tmp_path))


@pytest.mark.parametrize("name", ["hermetic-x\n", "x" * (MAX_PROJECT_CHARS + 1)])
def test_a_project_name_with_a_newline_or_over_the_limit_is_refused(
    tmp_path: Path, name: str
) -> None:
    text = yaml.safe_dump({"name": name, "services": {"db": good_service()}})
    assert any(p.startswith("name:") for p in problems_of(text, tmp_path))


def test_the_longest_service_id_still_gives_a_file_that_passes_its_own_check(
    tmp_path: Path,
) -> None:
    text = generate_compose(deps_set(dep("x", "redis"), service="a" * 63), tmp_path)
    assert len(PROJECT_PREFIX + "a" * 63) == MAX_PROJECT_CHARS
    assert check_compose(text, tmp_path)["name"] == f"{PROJECT_PREFIX}{'a' * 63}"


def test_a_service_name_with_a_newline_is_refused(tmp_path: Path) -> None:
    text = yaml.safe_dump({"services": {"db\n": good_service()}})
    assert any("lowercase slug" in p for p in problems_of(text, tmp_path))


@pytest.mark.parametrize("text", ["- a\n- b\n", "just text\n", ""])
def test_the_file_must_be_a_mapping(tmp_path: Path, text: str) -> None:
    assert problems_of(text, tmp_path) == ["the file must be a mapping with a `services` key"]


@pytest.mark.parametrize("services", [None, {}, [], "db"])
def test_there_must_be_services(tmp_path: Path, services: object) -> None:
    text = yaml.safe_dump({"services": services})
    assert any("at least one service" in p for p in problems_of(text, tmp_path))


def test_a_service_name_must_be_a_slug_and_a_body_a_mapping(tmp_path: Path) -> None:
    text = yaml.safe_dump({"services": {"Bad_Name": good_service(), "ok": "redis", 5: {}}})
    problems = problems_of(text, tmp_path)
    assert any("Bad_Name" in p and "lowercase slug" in p for p in problems)
    assert any("services.ok: must be a mapping" in p for p in problems)
    assert any("services.5" in p for p in problems)


def test_too_many_services_are_refused(tmp_path: Path) -> None:
    services = {f"s{i}": good_service() for i in range(MAX_DEPENDENCIES + 1)}
    text = yaml.safe_dump({"services": services})
    assert any("more than" in p for p in problems_of(text, tmp_path))


@pytest.mark.parametrize("name", ["Upper", "has space", "", 5])
def test_the_project_name_must_be_a_plain_lowercase_name(tmp_path: Path, name: object) -> None:
    text = yaml.safe_dump({"name": name, "services": {"db": good_service()}})
    assert any(p.startswith("name:") for p in problems_of(text, tmp_path))


def test_a_file_over_the_size_limit_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    assert "larger than" in problems_of("#" * (MAX_COMPOSE_BYTES + 1), tmp_path)[0]


def test_every_problem_is_reported_at_once_and_in_a_stable_order(tmp_path: Path) -> None:
    text = document_with(privileged=True, image="ubuntu", restart="always")
    problems = problems_of(text, tmp_path)
    assert len(problems) == 3 and problems == sorted(problems)


def test_checking_the_same_text_twice_gives_the_same_result(tmp_path: Path) -> None:
    text = document_with()
    first = check_compose(text, tmp_path)
    assert check_compose(text, tmp_path) == copy.deepcopy(first)


# --- the commands ---


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    write_dependencies(tmp_path / "tests" / "shop", ALL_KINDS)
    return tmp_path


def test_compose_writes_the_file_where_provisioning_will_look(project: Path) -> None:
    result = runner.invoke(app, ["compose", "--service", "shop"])
    assert result.exit_code == 0, result.output
    written = project / ".hermetic" / "provision" / "shop" / COMPOSE_NAME
    assert written.read_text() == GOLDEN.read_text()
    assert f"wrote {Path('.hermetic/provision/shop') / COMPOSE_NAME} (5 services)" in result.output


def test_compose_takes_a_suite_and_an_output(project: Path) -> None:
    out = project / "elsewhere" / "stack.yml"
    suite = project / "tests" / "shop"
    args = ["compose", "--service", "shop", "--suite", str(suite), "--output", str(out)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert out.read_text() == GOLDEN.read_text()


def test_compose_without_dependencies_says_to_run_discover(project: Path) -> None:
    result = runner.invoke(app, ["compose", "--service", "other"])
    assert result.exit_code == 2 and "run `hermetic discover` first" in result.output


def test_compose_with_an_empty_set_is_an_error(project: Path) -> None:
    write_dependencies(project / "tests" / "empty", deps_set(service="empty"))
    result = runner.invoke(app, ["compose", "--service", "empty"])
    assert result.exit_code == 2 and "no dependencies to provision" in result.output


def test_compose_rejects_a_bad_service_name(project: Path) -> None:
    result = runner.invoke(app, ["compose", "--service", "Bad Name"])
    assert result.exit_code == 2 and "invalid service" in result.output


def test_compose_reports_a_file_it_cannot_write(project: Path) -> None:
    (project / "blocker").write_text("a file, not a directory")
    out = project / "blocker" / "stack.yml"
    result = runner.invoke(app, ["compose", "--service", "shop", "--output", str(out)])
    assert result.exit_code == 2 and "error:" in result.output


def test_check_compose_accepts_what_compose_writes(project: Path) -> None:
    assert runner.invoke(app, ["compose", "--service", "shop"]).exit_code == 0
    path = project / ".hermetic" / "provision" / "shop" / COMPOSE_NAME
    result = runner.invoke(app, ["check-compose", str(path)])
    assert result.exit_code == 0 and "(5 services)" in result.output


def test_check_compose_refuses_with_every_problem_and_exit_1(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text(document_with(privileged=True, image="ubuntu"))
    result = runner.invoke(app, ["check-compose", str(path)])
    assert result.exit_code == 1
    assert "refused: services.db.privileged: not allowed" in result.output
    assert "refused: services.db.image:" in result.output


def test_check_compose_resolves_mounts_against_the_files_own_directory(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "stubs").mkdir()
    (tmp_path / "b" / "stack.yml").write_text(document_with(volumes=["./../a/stubs:/m:ro"]))
    result = runner.invoke(app, ["check-compose", str(tmp_path / "b" / "stack.yml")])
    assert result.exit_code == 1 and "outside the working directory" in result.output


def test_check_compose_reports_an_unreadable_file_as_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check-compose", str(tmp_path / "missing.yml")])
    assert result.exit_code == 2 and "cannot read" in result.output
