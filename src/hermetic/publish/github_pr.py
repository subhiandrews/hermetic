"""Propose a generated suite to a GitHub repository as a pull request.

The token, the host it may reach and what is written are described under "Publishing to GitHub"
in docs/architecture.md.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from hermetic.net import is_header_token
from hermetic.publish.archive import read_suite
from hermetic.publish.publishers import PublisherError

DEFAULT_API_URL = "https://api.github.com"
REPO_VARIABLE = "HERMETIC_GITHUB_REPO"
TOKEN_VARIABLE = "HERMETIC_GITHUB_TOKEN"
BASE_VARIABLE = "HERMETIC_GITHUB_BASE"
API_VARIABLE = "HERMETIC_GITHUB_API"
_MESSAGE_CHARS = 200

TokenSource = Callable[[], str]
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")


def _is_branch(name: str) -> bool:
    if not _BRANCH.fullmatch(name) or ".." in name or "//" in name:
        return False
    return not name.endswith(("/", ".lock", "."))


def _text(data: object, *path: str) -> str:
    """The string at `path` in a GitHub reply, or PublisherError when the reply has no such."""
    node = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, str) or not node:
        raise PublisherError(f"GitHub sent a reply without {'.'.join(path)}")
    return node


def _address(pull: object) -> str:
    """The `html_url` of a pull request, refused when it holds anything a terminal would act on."""
    url = _text(pull, "html_url")
    if not url.isprintable() or any(c.isspace() for c in url):
        raise PublisherError("GitHub sent a pull request address that is not valid")
    return url


class GitHubPRPublisher:
    """Puts the suite on a new branch of `repo` and opens a pull request for it.

    It writes only `tests/<service>/`, never touches the base branch and never force-pushes. The
    branch is named for the suite's content, so publishing the same suite again finds the pull
    request that is already open instead of making another.
    """

    def __init__(
        self,
        repo: str,
        token_source: TokenSource,
        *,
        base: str | None = None,
        api_url: str = DEFAULT_API_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not _REPO.fullmatch(repo) or {".", ".."} & set(repo.split("/")):
            raise PublisherError(f"{REPO_VARIABLE} must look like owner/name, not {repo!r}")
        if base is not None and not _is_branch(base):
            raise PublisherError(f"{BASE_VARIABLE} is not a branch name: {base!r}")
        try:
            parts = urlsplit(api_url)
            host = parts.hostname
            parts.port  # noqa: B018  (reading it validates the port; it raises for a bad one)
        except ValueError as exc:
            raise PublisherError(f"{API_VARIABLE} is not an address") from exc
        if parts.scheme != "https" or not host or parts.username or parts.password:
            raise PublisherError(f"{API_VARIABLE} must be an https address without credentials")
        self._repo = repo
        self._base = base
        self._token_source = token_source
        self._where = host
        self._injected = client
        self._client = client
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout

    @contextlib.contextmanager
    def _session(self) -> Iterator[None]:
        """Holds an HTTP client for one publish; one this class built is closed afterwards."""
        if self._injected is not None:
            yield
            return
        # trust_env=False: a proxy named in the environment must not see the token.
        with httpx.Client(
            base_url=self._api_url, timeout=self._timeout, follow_redirects=False, trust_env=False
        ) as client:
            self._client = client
            try:
                yield
            finally:
                self._client = None

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> GitHubPRPublisher:
        env = os.environ if env is None else env
        repo = env.get(REPO_VARIABLE)
        api_url = env.get(API_VARIABLE) or DEFAULT_API_URL
        # The workflow's token is a fallback only inside Actions and only for github.com: anywhere
        # else GITHUB_TOKEN is whatever another tool put there, and it must not travel here.
        in_actions = env.get("GITHUB_ACTIONS") == "true" and api_url == DEFAULT_API_URL
        token = env.get(TOKEN_VARIABLE) or (env.get("GITHUB_TOKEN") if in_actions else None)
        if not repo:
            raise PublisherError(f"set {REPO_VARIABLE} to the repository, as owner/name")
        if not token:
            raise PublisherError(f"set {TOKEN_VARIABLE} to a token that may write that repository")
        return cls(
            repo,
            lambda: token,
            base=env.get(BASE_VARIABLE) or None,
            api_url=api_url,
        )

    def publish(self, service: str, suite_dir: Path) -> str:
        files = read_suite(suite_dir)
        with self._session():
            return self._publish(service, files)

    def _publish(self, service: str, files: list[tuple[str, bytes]]) -> str:
        token = self._token_source()
        if not token or not is_header_token(token):
            raise PublisherError("the GitHub token cannot be sent: use printable ASCII, no spaces")
        branch = f"hermetic/{service}-{_digest(files)}"
        repo = f"repos/{self._repo}"
        head = quote(branch, safe="/")

        base = self._base or _text(self._json(token, "GET", repo), "default_branch")
        owner = self._repo.split("/")[0]
        open_prs = self._json(
            token, "GET", f"{repo}/pulls", params={"state": "open", "head": f"{owner}:{branch}"}
        )
        if isinstance(open_prs, list) and open_prs:
            found = _address(open_prs[0])
            if _text(open_prs[0], "base", "ref") != base:
                raise PublisherError(f"{found} is open for this suite but targets another branch")
            return found
        base_ref = quote(base, safe="/")
        base_sha = _text(
            self._json(token, "GET", f"{repo}/git/ref/heads/{base_ref}"), "object", "sha"
        )
        existing = self._call(token, "GET", f"{repo}/git/ref/heads/{head}", accept=(404,))
        if existing.status_code == 404:
            self._create_branch(token, service, branch, base_sha, files)
        elif self._holds_only_the_suite(token, service, branch, base_sha, files):
            raise PublisherError(f"{base} already has what branch {branch} holds")
        response = self._call(
            token,
            "POST",
            f"{repo}/pulls",
            accept=(422,),
            body={
                "title": f"Add Hermetic tests for {service}",
                "head": branch,
                "base": base,
                "body": f"Generated by Hermetic for `{service}`: {len(files)} files under "
                f"`tests/{service}/`.",
            },
        )
        if response.status_code == 422:
            raise PublisherError(self._refusal(422, response, token))
        try:
            pull = response.json()
        except ValueError as exc:
            raise PublisherError(f"{self._where} sent a reply that is not valid") from exc
        return _address(pull)

    def _holds_only_the_suite(
        self, token: str, service: str, branch: str, base_sha: str, files: list[tuple[str, bytes]]
    ) -> bool:
        """Whether the base already has the branch's commits; refuses a branch with anything else.

        A reused branch should be one Hermetic made earlier, but anyone who can push may have added
        to it, so it must be exactly one commit on `base_sha` that adds this suite's files.
        """
        repo = f"repos/{self._repo}"
        head = quote(branch, safe="/")
        diff = self._json(token, "GET", f"{repo}/compare/{base_sha}...{head}")
        if diff.get("ahead_by") == 0 if isinstance(diff, dict) else False:
            return True  # the branch is part of the base's history: it was merged
        found = {}
        listed = diff.get("files") if isinstance(diff, dict) else None
        if isinstance(listed, list):
            found = {
                f.get("filename"): f.get("sha")
                for f in listed
                if isinstance(f, dict) and f.get("status") in ("added", "modified")
            }
            if len(found) != len(listed):
                found = {}
        expected = {f"tests/{service}/{name}": _blob_sha(data) for name, data in files}
        one_commit = (
            isinstance(diff, dict)
            and diff.get("ahead_by") == 1
            and diff.get("behind_by") == 0
            and _text(diff, "merge_base_commit", "sha") == base_sha
        )
        if not one_commit or found != expected:
            raise PublisherError(
                f"branch {branch} holds more or other than this suite on {base_sha[:12]}; "
                "delete it or publish from a changed suite"
            )
        return False

    def _create_branch(
        self, token: str, service: str, branch: str, base_sha: str, files: list[tuple[str, bytes]]
    ) -> None:
        repo = f"repos/{self._repo}"
        base_tree = _text(self._json(token, "GET", f"{repo}/git/commits/{base_sha}"), "tree", "sha")
        entries = []
        for name, data in files:
            blob = self._json(
                token,
                "POST",
                f"{repo}/git/blobs",
                body={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
            )
            entries.append(
                {
                    "path": f"tests/{service}/{name}",
                    "mode": "100644",
                    "type": "blob",
                    "sha": _text(blob, "sha"),
                }
            )
        tree = self._json(
            token, "POST", f"{repo}/git/trees", body={"base_tree": base_tree, "tree": entries}
        )
        commit = self._json(
            token,
            "POST",
            f"{repo}/git/commits",
            body={
                "message": f"Add Hermetic tests for {service}",
                "tree": _text(tree, "sha"),
                "parents": [base_sha],
            },
        )
        self._json(
            token,
            "POST",
            f"{repo}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": _text(commit, "sha")},
        )

    def _json(
        self,
        token: str,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        response = self._call(token, method, path, body=body, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise PublisherError(f"{self._where} sent a reply that is not valid") from exc

    def _call(
        self,
        token: str,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        accept: tuple[int, ...] = (),
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            assert self._client is not None  # only called within `_session`
            response = self._client.request(
                method, f"/{path}", json=body, params=params, headers=headers
            )
        except httpx.HTTPError as exc:  # the message can quote the URL, so only the kind is kept
            raise PublisherError(f"cannot reach {self._where} ({type(exc).__name__})") from exc
        status = response.status_code
        if 200 <= status < 300 or status in accept:  # redirects are not followed
            return response
        raise PublisherError(self._refusal(status, response, token))

    def _refusal(self, status: int, response: httpx.Response, token: str) -> str:
        if status == 401:
            return f"{self._where} refused the token (HTTP 401): check {TOKEN_VARIABLE}"
        if status in (403, 429):
            return (
                f"{self._where} refused the request (HTTP {status}): the token cannot write "
                f"{self._repo}, or the rate limit is reached"
            )
        if status == 404:
            return (
                f"{self._repo} or a branch in it was not found (HTTP 404), "
                "or the token cannot see it"
            )
        detail = self._reasons(response, token)
        return f"{self._where} answered HTTP {status}" + (f": {detail}" if detail else "")

    @staticmethod
    def _reasons(response: httpx.Response, token: str) -> str:
        """What GitHub said was wrong, as one line that is safe to print."""
        try:
            data = response.json()
            parts = [data.get("message", "")]
            parts += [e.get("message", "") for e in data.get("errors", []) if isinstance(e, dict)]
        except (ValueError, AttributeError, TypeError):
            return ""
        text = "; ".join(str(part) for part in parts if part).replace(token, "[REDACTED]")
        return "".join(c for c in text if c.isprintable())[:_MESSAGE_CHARS]


def _blob_sha(data: bytes) -> str:
    """The id git gives a file with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _digest(files: list[tuple[str, bytes]]) -> str:
    """A short fingerprint of the suite's names and bytes, so equal suites share a branch."""
    hasher = hashlib.sha256()
    for name, data in files:
        hasher.update(f"{name}\0{len(data)}\0".encode())
        hasher.update(data)
    return hasher.hexdigest()[:12]
