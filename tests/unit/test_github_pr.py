from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.publish import BUILTIN_PUBLISHERS, ExportError, PublisherError, load_publisher
from hermetic.publish.github_pr import GitHubPRPublisher

TOKEN = "ghp_markerTOKEN0123456789"
REPO = "acme/service-tests"
PR_URL = "https://github.com/acme/service-tests/pull/7"
WRITES = {
    f"POST repos/{REPO}/{part}"
    for part in ("git/blobs", "git/trees", "git/commits", "git/refs", "pulls")
}

runner = CliRunner()


def make_suite(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "test_a.py").write_text("# a\n")
    (root / ".hermetic").mkdir()
    (root / ".hermetic" / "manifest.json").write_text("{}")
    (root / "notes.txt").write_text("not generated")
    return root


class FakeGitHub:
    """Just enough of GitHub's REST API for one publish, recording what it was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.open_prs: list[dict[str, Any]] = []
        self.branch_exists = False
        self.compare: Any = None  # None: what a clean earlier run left
        self.replies: dict[str, httpx.Response] = {}  # "METHOD path" -> a forced reply
        self.created: dict[str, list[dict[str, object]]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/")
        key = f"{request.method} {path}"
        if key in self.replies:
            return self.replies[key]
        if request.method == "POST":
            self.created.setdefault(path, []).append(json.loads(request.content))
        if path.startswith(f"repos/{REPO}/git/ref/heads/hermetic/"):
            return httpx.Response(200 if self.branch_exists else 404, json={})
        if "/compare/" in path:
            return httpx.Response(200, json=self.compare or self.clean_compare())
        routes = {
            f"GET repos/{REPO}/pulls": httpx.Response(200, json=self.open_prs),
            f"GET repos/{REPO}": httpx.Response(200, json={"default_branch": "main"}),
            f"GET repos/{REPO}/git/ref/heads/main": httpx.Response(
                200, json={"object": {"sha": "base-sha"}}
            ),
            f"GET repos/{REPO}/git/commits/base-sha": httpx.Response(
                200, json={"tree": {"sha": "base-tree"}}
            ),
            f"POST repos/{REPO}/git/blobs": httpx.Response(201, json={"sha": "blob-sha"}),
            f"POST repos/{REPO}/git/trees": httpx.Response(201, json={"sha": "tree-sha"}),
            f"POST repos/{REPO}/git/commits": httpx.Response(201, json={"sha": "commit-sha"}),
            f"POST repos/{REPO}/git/refs": httpx.Response(201, json={}),
            f"POST repos/{REPO}/pulls": httpx.Response(201, json={"html_url": PR_URL}),
        }
        return routes.get(key, httpx.Response(500, json={"message": f"unexpected {key}"}))

    @staticmethod
    def clean_compare() -> dict[str, Any]:
        def blob(data: bytes) -> str:
            return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()

        return {
            "ahead_by": 1,
            "behind_by": 0,
            "merge_base_commit": {"sha": "base-sha"},
            "files": [
                {"filename": "tests/petstore/test_a.py", "status": "added", "sha": blob(b"# a\n")},
                {
                    "filename": "tests/petstore/.hermetic/manifest.json",
                    "status": "added",
                    "sha": blob(b"{}"),
                },
            ],
        }

    def publisher(self, base: str | None = None) -> GitHubPRPublisher:
        client = httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(self)
        )
        return GitHubPRPublisher(REPO, lambda: TOKEN, base=base, client=client)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/')}" for r in self.requests]


def test_a_suite_becomes_a_branch_and_a_pull_request(tmp_path: Path) -> None:
    github = FakeGitHub()
    url = github.publisher().publish("petstore", make_suite(tmp_path / "suite"))
    assert url == PR_URL

    (tree,) = github.created[f"repos/{REPO}/git/trees"]
    assert tree["base_tree"] == "base-tree"
    entries = tree["tree"]
    assert isinstance(entries, list)
    assert sorted(entry["path"] for entry in entries) == [
        "tests/petstore/.hermetic/manifest.json",
        "tests/petstore/test_a.py",
    ]  # the notes file is not part of the suite
    blobs = github.created[f"repos/{REPO}/git/blobs"]
    assert {base64.b64decode(str(b["content"])) for b in blobs} == {b"# a\n", b"{}"}
    (commit,) = github.created[f"repos/{REPO}/git/commits"]
    assert commit["parents"] == ["base-sha"] and commit["tree"] == "tree-sha"
    (ref,) = github.created[f"repos/{REPO}/git/refs"]
    assert str(ref["ref"]).startswith("refs/heads/hermetic/petstore-")
    assert ref["sha"] == "commit-sha"
    (pull,) = github.created[f"repos/{REPO}/pulls"]
    assert pull["base"] == "main" and ref["ref"] == f"refs/heads/{pull['head']}"


def test_nothing_is_written_to_the_base_branch_and_nothing_is_forced(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.publisher().publish("petstore", make_suite(tmp_path / "suite"))
    assert {path for path in github.paths() if not path.startswith("GET")} == WRITES
    (ref,) = github.created[f"repos/{REPO}/git/refs"]
    assert ref["ref"] != "refs/heads/main" and "force" not in ref


def test_publishing_the_same_suite_again_returns_the_open_pull_request(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.open_prs = [{"html_url": PR_URL, "base": {"ref": "main"}}]
    assert github.publisher().publish("petstore", make_suite(tmp_path / "s")) == PR_URL
    assert not [path for path in github.paths() if path.startswith("POST")]


def test_an_existing_branch_with_no_open_pull_request_gets_only_a_pull_request(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.branch_exists = True
    assert github.publisher().publish("petstore", make_suite(tmp_path / "s")) == PR_URL
    assert [path for path in github.paths() if path.startswith("POST")] == [
        f"POST repos/{REPO}/pulls"
    ]


def test_an_open_pull_request_aimed_at_another_branch_is_not_returned_as_the_result(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.open_prs = [{"html_url": PR_URL, "base": {"ref": "release"}}]
    with pytest.raises(PublisherError, match="targets another branch"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def test_a_suite_whose_pull_request_was_merged_says_the_base_already_has_it(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.branch_exists = True
    github.compare = {"ahead_by": 0, "behind_by": 3}
    with pytest.raises(PublisherError, match=r"main already has what branch hermetic/"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def _tampered() -> list[dict[str, Any]]:
    clean = FakeGitHub.clean_compare()
    extra = {"filename": ".github/workflows/x.yml", "status": "added", "sha": "f" * 40}
    changed = {**clean, "files": [*clean["files"], extra]}
    wrong = {**clean, "files": [{**clean["files"][0], "sha": "0" * 40}, clean["files"][1]]}
    removed = {**clean, "files": [{**f, "status": "removed"} for f in clean["files"]]}
    return [
        changed,
        wrong,
        removed,
        {**clean, "ahead_by": 2},
        {**clean, "behind_by": 1},
        {**clean, "merge_base_commit": {"sha": "other"}},
        {**clean, "files": "none"},
        {**clean, "files": [3]},
    ]


@pytest.mark.parametrize("compare", _tampered())
def test_a_branch_that_holds_more_than_the_suite_is_not_opened_as_a_pull_request(
    tmp_path: Path, compare: dict[str, Any]
) -> None:
    github = FakeGitHub()
    github.branch_exists = True
    github.compare = compare
    with pytest.raises(PublisherError, match="holds more or other than this suite"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))
    assert not [path for path in github.paths() if path.startswith("POST")]


def test_a_comparison_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.branch_exists = True
    github.compare = [1]
    with pytest.raises(PublisherError, match="holds more or other than this suite"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def test_a_client_the_publisher_built_is_closed_and_built_again_for_the_next_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = FakeGitHub()
    real = httpx.Client
    built: list[httpx.Client] = []

    def make(**kwargs: Any) -> httpx.Client:
        client = real(transport=httpx.MockTransport(github), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", make)
    publisher = GitHubPRPublisher(REPO, lambda: TOKEN)
    suite = make_suite(tmp_path / "s")
    publisher.publish("petstore", suite)
    github.branch_exists = True
    publisher.publish("petstore", suite)
    assert len(built) == 2 and all(client.is_closed for client in built)


def test_any_other_refusal_to_open_the_pull_request_is_reported_with_its_reasons(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.replies[f"POST repos/{REPO}/pulls"] = httpx.Response(
        422, json={"message": "Validation Failed", "errors": [{"message": "head is bad"}, 3]}
    )
    with pytest.raises(PublisherError, match="Validation Failed; head is bad"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def test_a_pull_request_reply_that_is_not_json_is_refused(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.replies[f"POST repos/{REPO}/pulls"] = httpx.Response(201, text="not json")
    with pytest.raises(PublisherError, match="not valid"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


@pytest.mark.parametrize("url", ["https://x/\x1b[2Jpull/1", "https://x/pull 1", "https://x/\npull"])
def test_a_pull_request_address_a_terminal_would_act_on_is_refused(
    tmp_path: Path, url: str
) -> None:
    github = FakeGitHub()
    github.replies[f"POST repos/{REPO}/pulls"] = httpx.Response(201, json={"html_url": url})
    with pytest.raises(PublisherError, match="address that is not valid"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def test_a_changed_suite_gets_another_branch(tmp_path: Path) -> None:
    github = FakeGitHub()
    suite = make_suite(tmp_path / "s")
    github.publisher().publish("petstore", suite)
    (suite / "test_a.py").write_text("# changed\n")
    github.publisher().publish("petstore", suite)
    refs = [str(r["ref"]) for r in github.created[f"repos/{REPO}/git/refs"]]
    assert len(set(refs)) == 2


def test_the_base_branch_can_be_chosen(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.replies[f"GET repos/{REPO}/git/ref/heads/release/1.x"] = httpx.Response(
        200, json={"object": {"sha": "base-sha"}}
    )
    github.publisher(base="release/1.x").publish("petstore", make_suite(tmp_path / "s"))
    (pull,) = github.created[f"repos/{REPO}/pulls"]
    assert pull["base"] == "release/1.x"
    assert f"GET repos/{REPO}" not in github.paths()  # the default branch was not needed


def test_the_token_travels_only_as_a_header_to_the_api_host_over_https(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.publisher().publish("petstore", make_suite(tmp_path / "s"))
    for request in github.requests:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.host == "api.github.com" and request.url.scheme == "https"
        assert TOKEN not in str(request.url) and TOKEN.encode() not in request.content


# --- failures ---


def refuse_blobs(github: FakeGitHub, response: httpx.Response) -> None:
    github.replies[f"POST repos/{REPO}/git/blobs"] = response


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (401, "Bad credentials", "refused the token (HTTP 401)"),
        (403, "forbidden", "refused the request (HTTP 403)"),
        (429, "slow down", "refused the request (HTTP 429)"),
        (404, "Not Found", "was not found (HTTP 404)"),
        (422, "Validation Failed", "answered HTTP 422: Validation Failed"),
        (500, "", "answered HTTP 500"),
    ],
)
def test_a_refusal_says_what_happened(
    tmp_path: Path, status: int, message: str, expected: str
) -> None:
    github = FakeGitHub()
    refuse_blobs(github, httpx.Response(status, json={"message": message}))
    with pytest.raises(PublisherError) as raised:
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))
    assert expected in str(raised.value)


def test_a_failure_message_has_its_control_characters_removed(tmp_path: Path) -> None:
    github = FakeGitHub()
    refuse_blobs(github, httpx.Response(422, json={"message": "bad\x1b[2J\nnews"}))
    with pytest.raises(PublisherError) as raised:
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))
    assert "\x1b" not in str(raised.value) and "\n" not in str(raised.value)


def test_no_failure_message_carries_the_token(tmp_path: Path) -> None:
    github = FakeGitHub()
    refuse_blobs(github, httpx.Response(422, json={"message": f"the token {TOKEN} is no good"}))
    with pytest.raises(PublisherError) as raised:
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))
    assert TOKEN not in str(raised.value) and "[REDACTED]" in str(raised.value)


@pytest.mark.parametrize(
    "reply", [httpx.Response(502, text="<html>bad</html>"), httpx.Response(502, json=[1])]
)
def test_a_failure_body_that_is_not_a_message_still_reports_the_status(
    tmp_path: Path, reply: httpx.Response
) -> None:
    github = FakeGitHub()
    refuse_blobs(github, reply)
    with pytest.raises(PublisherError, match=r"HTTP 502$"):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def test_a_transport_failure_names_the_kind_and_not_the_url(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url} with {TOKEN}")

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(boom))
    publisher = GitHubPRPublisher(REPO, lambda: TOKEN, client=client)
    with pytest.raises(PublisherError, match=r"api\.github\.com \(ConnectError\)") as raised:
        publisher.publish("petstore", make_suite(tmp_path / "s"))
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    "reply", [httpx.Response(200, text="not json"), httpx.Response(200, json={})]
)
def test_a_reply_that_is_not_what_github_sends_is_refused(
    tmp_path: Path, reply: httpx.Response
) -> None:
    github = FakeGitHub()
    github.replies[f"GET repos/{REPO}"] = reply
    with pytest.raises(PublisherError):
        github.publisher().publish("petstore", make_suite(tmp_path / "s"))


def test_a_suite_with_no_tests_is_refused_before_any_request(tmp_path: Path) -> None:
    github = FakeGitHub()
    (tmp_path / "empty").mkdir()
    with pytest.raises(ExportError, match="no generated tests"):
        github.publisher().publish("petstore", tmp_path / "empty")
    assert github.requests == []


@pytest.mark.parametrize("token", ["", "has space", "new\nline", "café"])
def test_a_token_that_cannot_travel_in_a_header_is_refused_without_quoting_it(
    tmp_path: Path, token: str
) -> None:
    github = FakeGitHub()
    client = httpx.Client(base_url="https://x", transport=httpx.MockTransport(github))
    publisher = GitHubPRPublisher(REPO, lambda: token, client=client)
    with pytest.raises(PublisherError, match="cannot be sent") as raised:
        publisher.publish("petstore", make_suite(tmp_path / "s"))
    assert (token or "\0") not in str(raised.value)
    assert github.requests == []


# --- configuration ---


def test_it_is_configured_from_the_environment() -> None:
    publisher = GitHubPRPublisher.from_environment(
        {
            "HERMETIC_GITHUB_REPO": REPO,
            "HERMETIC_GITHUB_TOKEN": TOKEN,
            "HERMETIC_GITHUB_BASE": "dev",
        }
    )
    assert isinstance(publisher, GitHubPRPublisher)


def test_inside_actions_the_workflow_token_is_the_fallback() -> None:
    publisher = GitHubPRPublisher.from_environment(
        {"HERMETIC_GITHUB_REPO": REPO, "GITHUB_TOKEN": TOKEN, "GITHUB_ACTIONS": "true"}
    )
    assert publisher._token_source() == TOKEN


@pytest.mark.parametrize(
    "extra",
    [
        {},  # a developer machine: GITHUB_TOKEN belongs to some other tool
        {"GITHUB_ACTIONS": "false"},
        {"GITHUB_ACTIONS": "true", "HERMETIC_GITHUB_API": "https://ghe.example.com/api/v3"},
    ],
)
def test_the_workflow_token_is_not_used_anywhere_else(extra: dict[str, str]) -> None:
    values = {"HERMETIC_GITHUB_REPO": REPO, "GITHUB_TOKEN": TOKEN, **extra}
    with pytest.raises(PublisherError, match="HERMETIC_GITHUB_TOKEN"):
        GitHubPRPublisher.from_environment(values)


def test_the_process_environment_is_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMETIC_GITHUB_REPO", REPO)
    monkeypatch.setenv("HERMETIC_GITHUB_TOKEN", TOKEN)
    assert isinstance(GitHubPRPublisher.from_environment(), GitHubPRPublisher)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"HERMETIC_GITHUB_TOKEN": TOKEN}, "HERMETIC_GITHUB_REPO"),
        ({"HERMETIC_GITHUB_REPO": REPO}, "HERMETIC_GITHUB_TOKEN"),
        ({"HERMETIC_GITHUB_REPO": "no-slash", "HERMETIC_GITHUB_TOKEN": TOKEN}, "owner/name"),
        ({"HERMETIC_GITHUB_REPO": "a/b/c", "HERMETIC_GITHUB_TOKEN": TOKEN}, "owner/name"),
        (
            {
                "HERMETIC_GITHUB_REPO": REPO,
                "HERMETIC_GITHUB_TOKEN": TOKEN,
                "HERMETIC_GITHUB_BASE": "../x",
            },
            "not a branch name",
        ),
    ],
)
def test_a_bad_configuration_says_what_to_set(values: dict[str, str], message: str) -> None:
    with pytest.raises(PublisherError, match=message):
        GitHubPRPublisher.from_environment(values)


@pytest.mark.parametrize("repo", ["acme/tests\n", "../x", "a/..", "./x", "a/b/c", "a"])
def test_a_repository_name_that_is_not_plain_is_refused(repo: str) -> None:
    with pytest.raises(PublisherError, match="owner/name"):
        GitHubPRPublisher(repo, lambda: TOKEN)


@pytest.mark.parametrize(
    "base", ["main\n", "a..b", "a//b", "a/", "x.lock", "-lead", "has space", ".hid", "a."]
)
def test_an_unsafe_branch_name_is_refused(base: str) -> None:
    with pytest.raises(PublisherError, match="not a branch name"):
        GitHubPRPublisher(REPO, lambda: TOKEN, base=base)


@pytest.mark.parametrize(
    "api",
    [
        "http://api.github.com",
        "http://localhost:8080",
        "https://user:pw@api.github.com",
        "https://",
        "https://host:notaport",
        "ftp://api.github.com",
    ],
)
def test_the_api_address_must_be_https_without_credentials(api: str) -> None:
    with pytest.raises(PublisherError, match=r"https|address"):
        GitHubPRPublisher(REPO, lambda: TOKEN, api_url=api)


def test_an_enterprise_address_is_accepted_and_redirects_are_not_followed() -> None:
    publisher = GitHubPRPublisher(REPO, lambda: TOKEN, api_url="https://ghe.example.com/api/v3/")
    with publisher._session():
        client = publisher._client
        assert client is not None
    assert client.follow_redirects is False and client.trust_env is False
    assert str(client.base_url).startswith("https://ghe.example.com/api/v3")


def test_github_pr_is_a_builtin_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMETIC_GITHUB_REPO", REPO)
    monkeypatch.setenv("HERMETIC_GITHUB_TOKEN", TOKEN)
    assert isinstance(load_publisher("github-pr"), GitHubPRPublisher)


def test_the_cli_reports_a_missing_configuration_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HERMETIC_GITHUB_REPO", "HERMETIC_GITHUB_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    suite = make_suite(tmp_path / "suite")
    args = ["publish", "--service", "svc", "--suite", str(suite), "--publisher", "github-pr"]
    result = runner.invoke(app, args)
    assert result.exit_code == 2 and "HERMETIC_GITHUB_REPO" in result.output


def test_the_cli_prints_the_pull_request_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = FakeGitHub()
    monkeypatch.setitem(BUILTIN_PUBLISHERS, "github-pr", github.publisher)
    suite = make_suite(tmp_path / "suite")
    args = ["publish", "--service", "svc", "--suite", str(suite), "--publisher", "github-pr"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0 and f"published svc to {PR_URL}" in result.output
