"""Lets a test make a mock answer with an error the downstream's spec declares.

`hermetic stubs` writes each declared error as a state of a WireMock scenario (see Provision in
docs/architecture.md). This module switches those states. A test gets `hermetic_mocks["carts"]
.answer("getCart", 404)`; the call is made here, on the host, to three fixed admin paths. The
test never holds a client or a URL for the mock, so "tests may only call the target" still holds.

What can be switched is read from the running mock, from the `metadata` of the stubs Hermetic
wrote, so nothing depends on files on disk. A name or state that mock does not declare is an
error that lists what it does declare.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx

from hermetic.redact import redact_url

START_STATE = "Started"  # WireMock's name for a scenario's first state; the success stub answers
MAX_LISTING_BYTES = 10_000_000  # the most of a mock's stub listing that is read
MAX_NAMES_SHOWN = 20  # operations or states named in an error message
_SLUG = re.compile(r"[a-z0-9-]+")  # the scenario names `hermetic stubs` writes; safe in a path
_STATUS = re.compile(r"[45][0-9][0-9]")
_MOCK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class MockError(Exception):
    """A test asked a mock for something it cannot do, or the mock could not be reached."""


def check_mock(name: str, url: str) -> None:
    """Refuse a mock name or URL that could not be used, before any test runs."""
    if not _MOCK_NAME.fullmatch(name):
        raise MockError(f"invalid mock name {name!r}: use letters, digits, '-' and '_'")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:  # also an unbalanced IPv6 bracket
        raise MockError(f"invalid URL for mock {name!r}: {redact_url(url)!r}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise MockError(f"invalid URL for mock {name!r}: expected http(s)://host[:port]")


def _listed(names: Iterable[str]) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:MAX_NAMES_SHOWN])
    more = len(ordered) - MAX_NAMES_SHOWN
    return shown + (f", and {more} more" if more > 0 else "")


class MockControl:
    """One mock, which a test may switch between the error states its stubs declare."""

    def __init__(self, name: str, url: str, *, timeout: float) -> None:
        self.name = name
        self._url = url
        self.touched = False  # a state was switched, so it needs a reset
        self._declared: dict[str, tuple[str, set[str]]] | None = None
        # No proxy settings from the environment, no redirects: the mock is where it was told to be.
        self._client = httpx.Client(
            base_url=url, timeout=timeout, follow_redirects=False, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    def answer(self, operation: str, status: int) -> None:
        """Make the mock answer `operation` with the declared error `status` until reset."""
        scenario, states = self._operations().get(operation, ("", set()))
        if not states:
            raise MockError(
                f"mock {self.name!r} declares no error for operation {operation!r}; "
                f"operations with errors: {_listed(self._operations()) or 'none'}"
            )
        wanted = str(status)
        if wanted not in states:
            raise MockError(
                f"mock {self.name!r} does not declare {wanted} for {operation!r}; "
                f"declared: {_listed(states)}"
            )
        self.touched = True  # even a failed switch may have changed state, so it is reset
        self._call("PUT", f"/__admin/scenarios/{scenario}/state", {"state": wanted})

    def reset(self) -> None:
        """Return every scenario on the mock to its first state."""
        self._call("POST", "/__admin/scenarios/reset", None)
        self.touched = False

    def _call(self, method: str, path: str, body: dict[str, str] | None) -> httpx.Response:
        try:
            response = self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise MockError(
                f"cannot reach mock {self.name!r} at {redact_url(self._url)}: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise MockError(
                f"mock {self.name!r} answered {method} {path} with {response.status_code}"
            )
        return response

    def _operations(self) -> dict[str, tuple[str, set[str]]]:
        """Operation id to its scenario and the error states it has, read once from the mock."""
        if self._declared is None:
            self._declared = self._read_listing()
        return self._declared

    def _read_listing(self) -> dict[str, tuple[str, set[str]]]:
        response = self._call("GET", "/__admin/mappings", None)
        if len(response.content) > MAX_LISTING_BYTES:
            raise MockError(f"mock {self.name!r} lists more than {MAX_LISTING_BYTES} bytes")
        try:
            document = response.json()
            stubs = document["mappings"]
            total = document["meta"]["total"]
        except (ValueError, KeyError, TypeError) as exc:
            raise MockError(f"mock {self.name!r} did not return a stub listing") from exc
        if not isinstance(stubs, list) or not isinstance(total, int) or total > len(stubs):
            raise MockError(f"mock {self.name!r} did not list all of its stubs")
        found: dict[str, tuple[str, set[str]]] = {}
        for stub in stubs:
            metadata = stub.get("metadata") if isinstance(stub, dict) else None
            if not isinstance(metadata, dict) or metadata.get("hermetic") is not True:
                continue
            operation, scenario, state = (
                metadata.get(key) for key in ("operation", "scenario", "state")
            )
            if not (isinstance(operation, str) and isinstance(scenario, str)):
                continue
            if not _SLUG.fullmatch(scenario) or not isinstance(state, str):
                continue
            if state == START_STATE or not _STATUS.fullmatch(state):
                continue
            known, states = found.setdefault(operation, (scenario, set()))
            if known != scenario:
                raise MockError(f"mock {self.name!r} has two scenarios for operation {operation!r}")
            states.add(state)
        return found


class Mocks:
    """The mocks a run was given, by name; what a test receives as `hermetic_mocks`."""

    def __init__(self, controls: dict[str, MockControl]) -> None:
        self._controls = controls

    def __getitem__(self, name: str) -> MockControl:
        try:
            return self._controls[name]
        except KeyError:
            known = _listed(self._controls) or "none"
            raise MockError(
                f"no mock named {name!r}; known: {known}. Pass --mock NAME=URL to `hermetic run`"
            ) from None

    def finish(self) -> None:
        """Reset every mock a test switched, close the connections, then report a failure."""
        failure: MockError | None = None
        for control in self._controls.values():
            try:
                if control.touched:
                    control.reset()
            except MockError as exc:
                failure = failure or exc
            finally:
                control.close()
        if failure is not None:
            raise failure
