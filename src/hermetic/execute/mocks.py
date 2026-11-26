"""Lets a test make a mock answer with an error the downstream's spec declares.

A test is handed a control object, never a client or a URL for the mock, and this module makes
the few admin calls itself on the host. That is why "tests may only call the target" still
holds, and why a test cannot register a stub or proxy elsewhere. What can be switched is read
from the running mock (the `metadata` of the stubs Hermetic wrote), so nothing depends on files
on disk. A name or state the mock does not declare is an error that lists what it does.

Everything a mock sends back is untrusted: a reply is read as raw bytes, never decoded, and
stops at a size and a time limit.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx

from hermetic.redact import redact_url

START_STATE = "Started"  # WireMock's name for a scenario's first state; the success stub answers
MAX_LISTING_BYTES = 10_000_000  # the most of a mock's stub listing that is read
MAX_REPLY_BYTES = 65_536  # the most of any other reply; they are small and only their status counts
MAX_NAMES_SHOWN = 20  # operations or states named in an error message
_SLUG = re.compile(r"[a-z0-9-]+")  # the scenario names `hermetic stubs` writes; safe in a path
_STATUS = re.compile(r"[45][0-9][0-9]")
_MOCK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

Declared = dict[str, tuple[str, set[str]]]  # operation id -> its scenario and its error states


class MockError(Exception):
    """A test asked a mock for something it cannot do, or the mock could not be reached."""


def _client(name: str, url: str, timeout: float) -> httpx.Client:
    # No proxy settings from the environment, no redirects: the mock is where it was told to be.
    try:
        return httpx.Client(base_url=url, timeout=timeout, follow_redirects=False, trust_env=False)
    except httpx.InvalidURL as exc:  # for example an IPv4 address with a part over 255
        raise MockError(f"invalid URL for mock {name!r}: {redact_url(url)!r}") from exc


def check_mock_name(name: str) -> None:
    """Refuse a mock name a test could not use."""
    if not _MOCK_NAME.fullmatch(name):
        # not echoed: with a forgotten NAME= the "name" is the front of the URL, password and all
        raise MockError("invalid mock name: use letters, digits, '-' and '_' (expected NAME=URL)")


def check_mock(name: str, url: str) -> None:
    """Refuse a mock name or URL that could not be used, before any test runs."""
    check_mock_name(name)
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:  # also an unbalanced IPv6 bracket
        raise MockError(f"invalid URL for mock {name!r}: {redact_url(url)!r}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise MockError(f"invalid URL for mock {name!r}: expected http(s)://host[:port]")
    _client(name, url, 1.0).close()  # what the run will build, so what it refuses is the same


def reset_all(urls: dict[str, str], *, timeout: float) -> None:
    """Return every mock to its first state, so nothing an earlier run left behind leaks in.

    It also finds a mock that cannot be reached before the first test, not inside it.
    """
    for name, url in urls.items():
        control = MockControl(name, url, timeout=timeout)
        try:
            control.reset()
        finally:
            control.close()


def _listed(names: Iterable[str]) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:MAX_NAMES_SHOWN])
    more = len(ordered) - MAX_NAMES_SHOWN
    return shown + (f", and {more} more" if more > 0 else "")


class MockControl:
    """One mock, which a test may switch between the error states its stubs declare.

    `listings` holds what each mock declares and may be shared by the controls of a run: the
    stubs do not change while it runs, so the listing is fetched once per mock, not per test.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        timeout: float,
        listings: dict[str, Declared] | None = None,
    ) -> None:
        self.name = name
        self._url = url
        self._timeout = timeout
        self._listings = {} if listings is None else listings
        self.touched = False  # a state was switched, so it needs a reset
        self._client: httpx.Client | None = None  # made on the first call: most tests use none

    def close(self) -> None:
        if self._client is not None:
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

    def _call(
        self, method: str, path: str, body: dict[str, str] | None, *, limit: int | None = None
    ) -> bytes:
        """The reply's raw bytes; an error if it is not a 200, too long, or too slow."""
        cap = MAX_REPLY_BYTES if limit is None else limit
        if self._client is None:
            self._client = _client(self.name, self._url, self._timeout)
        deadline = time.monotonic() + self._timeout
        try:
            # Identity asked for, and raw bytes read: a compressed reply is never expanded here.
            with self._client.stream(
                method, path, json=body, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code != 200:
                    raise MockError(
                        f"mock {self.name!r} answered {method} {path} with {response.status_code}"
                    )
                received = bytearray()
                for chunk in response.iter_raw():
                    received += chunk
                    if len(received) > cap:
                        raise MockError(f"mock {self.name!r} sent more than {cap} bytes")
                    if time.monotonic() > deadline:  # the client's timeout is per read
                        raise MockError(f"mock {self.name!r} answered too slowly")
                return bytes(received)
        except httpx.HTTPError as exc:
            raise MockError(
                f"cannot reach mock {self.name!r} at {redact_url(self._url)}: {type(exc).__name__}"
            ) from exc

    def _operations(self) -> Declared:
        """What the mock declares, read from it once per run."""
        if self.name not in self._listings:
            self._listings[self.name] = self._read_listing()
        return self._listings[self.name]

    def _read_listing(self) -> Declared:
        raw = self._call("GET", "/__admin/mappings", None, limit=MAX_LISTING_BYTES)
        try:
            document = json.loads(raw)
            stubs = document["mappings"]
            total = document["meta"]["total"]
        except (ValueError, KeyError, TypeError, RecursionError) as exc:  # nesting can be deep
            raise MockError(f"mock {self.name!r} did not return a stub listing") from exc
        if not isinstance(stubs, list) or not isinstance(total, int) or total > len(stubs):
            raise MockError(f"mock {self.name!r} did not list all of its stubs")
        found: Declared = {}
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

    def pending(self) -> set[str]:
        """The mocks still switched: after `finish`, the ones whose reset failed."""
        return {name for name, control in self._controls.items() if control.touched}

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
