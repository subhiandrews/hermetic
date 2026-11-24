"""A RunStore over a Hermetic server's HTTP API, so runs can be kept centrally."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from hermetic.history.models import RunRecord, RunSummary, ServiceSummary
from hermetic.history.store import RunNotFoundError, RunStoreError, is_valid_run_id
from hermetic.redact import redact_url
from hermetic.service import is_valid_service

LOOPBACK = ("localhost", "127.0.0.1", "::1")
LIST_LIMIT = 10_000  # the server's largest page; a longer history lists its newest runs
Summaries = list[RunSummary]  # aliases: `list` is shadowed by the method inside the class
Services = list[ServiceSummary]
_SUMMARIES = TypeAdapter(Summaries)
_SERVICES = TypeAdapter(Services)


def _segment(value: str) -> str:
    return quote(value, safe="")


class HttpRunStore:
    """Talks to `hermetic serve`: `POST /api/services/<service>/runs` to save, `GET` to read.

    The token is sent as a bearer token and only over https, or to a host on this machine, so it
    cannot cross the network in the clear. Redirects are not followed, so it cannot be handed to
    another host. Error messages name the server without its credentials and never carry the
    response body.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        try:
            parts = urlsplit(base_url)
            hostname = parts.hostname
            parts.port  # noqa: B018  (reading it validates the port; it raises for a bad one)
        except ValueError as exc:
            raise RunStoreError(f"not a server address: {redact_url(base_url)}") from exc
        if parts.scheme not in ("http", "https") or not hostname:
            raise RunStoreError(f"not a server address: {redact_url(base_url)}")
        # Credentials in the address are sent too (as Basic auth), so they count like the token.
        carries_secret = token or parts.username or parts.password
        if carries_secret and parts.scheme == "http" and hostname not in LOOPBACK:
            raise RunStoreError(
                f"refusing to send credentials to {hostname} over plain http; use https"
            )
        self._where = hostname
        self._token = token
        try:
            # trust_env=False: a proxy named in the environment must not see the token, and
            # loopback is not exempt from it.
            self._client = client or httpx.Client(
                base_url=base_url, timeout=timeout, follow_redirects=False, trust_env=False
            )
        except httpx.InvalidURL as exc:
            raise RunStoreError(f"not a server address: {redact_url(base_url)}") from exc

    def save(self, record: RunRecord) -> None:
        response = self._send(
            "POST", f"/api/services/{_segment(record.service)}/runs", body=record.model_dump_json()
        )
        self._check(response)

    def get(self, service: str, run_id: str) -> RunRecord:
        if not (is_valid_service(service) and is_valid_run_id(run_id)):  # nothing can have it
            raise RunNotFoundError(f"no run {run_id!r} for service {service!r}")
        response = self._send("GET", f"/api/services/{_segment(service)}/runs/{_segment(run_id)}")
        if response.status_code == 404:
            raise RunNotFoundError(f"no run {run_id!r} for service {service!r}")
        self._check(response)
        try:
            return RunRecord.model_validate_json(response.content)
        except ValidationError as exc:
            raise RunStoreError(f"{self._where} sent a run that is not valid") from exc

    def list(self, service: str | None = None) -> Summaries:
        path = "/api/runs" if service is None else f"/api/services/{_segment(service)}/runs"
        response = self._send("GET", path, limit=LIST_LIMIT)
        if service is not None and response.status_code == 404:  # a name no service can have
            return []
        self._check(response)
        return self._parse(_SUMMARIES, response)

    def services(self) -> Services:
        response = self._send("GET", "/api/services")
        self._check(response)
        return self._parse(_SERVICES, response)

    def _send(
        self, method: str, path: str, *, body: str | None = None, limit: int | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        params = {"limit": limit} if limit is not None else None
        try:
            return self._client.request(method, path, content=body, params=params, headers=headers)
        except httpx.HTTPError as exc:  # the message can quote the URL, so only the kind is kept
            raise RunStoreError(f"cannot reach {self._where} ({type(exc).__name__})") from exc

    def _check(self, response: httpx.Response) -> None:
        status = response.status_code
        if status in (401, 403):
            raise RunStoreError(
                f"{self._where} refused the request (HTTP {status}): check the token"
            )
        if not 200 <= status < 300:  # redirects are not followed, so a 3xx stored nothing
            raise RunStoreError(f"{self._where} answered HTTP {status}")

    def _parse[T](self, adapter: TypeAdapter[T], response: httpx.Response) -> T:
        try:
            return adapter.validate_json(response.content)
        except ValidationError as exc:
            raise RunStoreError(f"{self._where} sent a reply that is not valid") from exc
