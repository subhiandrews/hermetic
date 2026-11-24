"""HttpRunStore specifics. The shared contract (tests/unit/test_run_store.py) covers behaviour."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hermetic.history import (
    API_TOKEN_ENV,
    FileRunStore,
    HttpRunStore,
    RunNotFoundError,
    RunStoreError,
    open_store,
)
from tests.factories import make_record

TOKEN = "tok-3f9a"


def store_over(handler: httpx.MockTransport, token: str | None = None) -> HttpRunStore:
    client = httpx.Client(base_url="https://hist.test", transport=handler)
    return HttpRunStore("https://hist.test", token=token, client=client)


def answer(status: int, body: str = "[]") -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=body))


def test_a_save_posts_the_record_with_the_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={})

    store_over(httpx.MockTransport(handler), token=TOKEN).save(make_record("r1", service="alpha"))
    (request,) = seen
    assert request.method == "POST" and request.url.path == "/api/services/alpha/runs"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"] == "application/json"
    assert b'"run_id":"r1"' in request.content


def test_no_token_means_no_authorization_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    assert store_over(httpx.MockTransport(handler)).list() == []
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("service", ["a/b", "../x", "", "Alpha"])
def test_a_listing_of_a_name_no_service_can_have_is_empty_without_asking(service: str) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    assert store_over(httpx.MockTransport(never)).list(service) == []


def test_a_404_for_a_real_service_name_is_an_error_not_an_empty_history() -> None:
    # A wrong path, a proxy prefix or a server without these routes must not look like "no runs".
    with pytest.raises(RunStoreError, match="answered HTTP 404"):
        store_over(answer(404)).list("alpha")


@pytest.mark.parametrize(("service", "run_id"), [("a/b", "r1"), ("alpha", "../x"), ("alpha", "")])
def test_ids_no_run_can_have_are_not_found_without_asking_the_server(
    service: str, run_id: str
) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    with pytest.raises(RunNotFoundError):
        store_over(httpx.MockTransport(never)).get(service, run_id)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_says_so_without_the_body(status: int) -> None:
    store = store_over(answer(status, "secret-body"), token=TOKEN)
    with pytest.raises(RunStoreError, match=f"refused the request \\(HTTP {status}\\)") as caught:
        store.save(make_record("r1"))
    assert "secret-body" not in str(caught.value) and TOKEN not in str(caught.value)


def test_another_error_status_is_reported_by_number_only() -> None:
    with pytest.raises(RunStoreError, match="answered HTTP 500") as caught:
        store_over(answer(500, "stack trace with secret-body")).services()
    assert "secret-body" not in str(caught.value)


def test_a_server_that_cannot_be_reached_is_a_store_error_naming_no_credentials() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused for http://user:pw@hist.test")

    with pytest.raises(RunStoreError, match=r"cannot reach hist\.test \(ConnectError\)") as caught:
        store_over(httpx.MockTransport(down)).list()
    assert "pw" not in str(caught.value)


@pytest.mark.parametrize("body", ["not json", '[{"run_id": 1}]', "{}"])
def test_a_reply_that_is_not_valid_is_a_store_error(body: str) -> None:
    with pytest.raises(RunStoreError, match="not valid"):
        store_over(answer(200, body)).list()
    with pytest.raises(RunStoreError, match="not valid"):
        store_over(answer(200, body)).get("alpha", "r1")


@pytest.mark.parametrize(
    ("asked", "sent"), [(("alpha", "r2"), "alpha/r1"), (("beta", "r1"), "alpha/r1")]
)
def test_a_server_that_sends_a_different_run_than_asked_for_is_refused(
    asked: tuple[str, str], sent: str
) -> None:
    service, run_id = sent.split("/")
    other = make_record(run_id, service=service).model_dump_json()
    with pytest.raises(RunStoreError, match="different run"):
        store_over(answer(200, other)).get(*asked)
    # and the run that was asked for is accepted
    assert store_over(answer(200, other)).get(service, run_id).run_id == run_id


@pytest.mark.parametrize("status", [200, 204, 301, 302, 307, 399])
def test_a_save_is_only_recorded_on_a_2xx_answer(status: int) -> None:
    store = store_over(answer(status, ""))
    if 200 <= status < 300:
        store.save(make_record("r1"))
    else:
        with pytest.raises(RunStoreError, match=f"answered HTTP {status}"):
            store.save(make_record("r1"))  # a proxy's redirect stored nothing


@pytest.mark.parametrize(
    "address",
    [
        "http://[hist",
        "http://hist.example:notaport",
        "http://hist.example:99999",
        "http://hist.example/\x00",  # passes urlsplit; the HTTP client refuses it
        "http://hist\x7f.example",
    ],
)
def test_a_malformed_address_is_a_store_error_not_a_traceback(address: str) -> None:
    with pytest.raises(RunStoreError, match="not a server address"):
        HttpRunStore(address)


@pytest.mark.parametrize("address", ["http://user:pw@hist.example", "http://:pw@hist.example"])
def test_credentials_in_the_address_are_not_sent_in_the_clear_either(address: str) -> None:
    with pytest.raises(RunStoreError, match="over plain http") as caught:
        HttpRunStore(address)
    assert "pw" not in str(caught.value)
    HttpRunStore(address.replace("http://", "https://"))  # encrypted: fine
    HttpRunStore(address.replace("hist.example", "127.0.0.1"))  # this machine: fine


def test_a_proxy_in_the_environment_never_sees_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    assert httpx.Client()._mounts  # the default client would route through the proxy
    store = HttpRunStore("http://127.0.0.1:8000", token=TOKEN)
    assert store._client._mounts == {}  # ours does not, so loopback is not sent through one


@pytest.mark.parametrize("token", ["tök3n", "tok en", "tok\nen", "tok\x00en", "tok\ten", "日本語"])
def test_a_token_that_cannot_be_sent_in_a_header_is_refused_without_quoting_it(token: str) -> None:
    with pytest.raises(RunStoreError, match="cannot be sent") as caught:
        HttpRunStore("https://hist.example", token=token)
    assert token not in str(caught.value)


def test_every_loopback_name_may_receive_the_token_in_the_clear() -> None:
    for name in ("localhost", "127.0.0.1", "[::1]"):  # as written in an address
        HttpRunStore(f"http://{name}:8000", token=TOKEN)


def test_the_token_is_not_sent_in_the_clear_to_another_machine() -> None:
    with pytest.raises(RunStoreError, match="over plain http"):
        HttpRunStore("http://hist.example", token=TOKEN)
    HttpRunStore("https://hist.example", token=TOKEN)  # encrypted: fine
    HttpRunStore("http://localhost:8000", token=TOKEN)  # this machine: fine
    HttpRunStore("http://hist.example")  # no token to leak: fine


@pytest.mark.parametrize("address", ["ftp://hist.example", "hist.example", "http://", ""])
def test_something_that_is_not_a_server_address_is_refused(address: str) -> None:
    with pytest.raises(RunStoreError, match="not a server address"):
        HttpRunStore(address)


def test_an_address_with_credentials_is_not_echoed() -> None:
    with pytest.raises(RunStoreError) as caught:
        HttpRunStore("ftp://user:pw@hist.example")
    assert "pw" not in str(caught.value)


def test_redirects_are_not_followed_so_the_token_cannot_be_handed_on() -> None:
    store = HttpRunStore("https://hist.example", token=TOKEN)
    assert store._client.follow_redirects is False


def test_open_store_picks_the_store_from_the_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    assert isinstance(open_store(tmp_path / "runs"), FileRunStore)
    assert isinstance(open_store(str(tmp_path)), FileRunStore)
    assert isinstance(open_store("http://127.0.0.1:8000"), HttpRunStore)
    assert isinstance(open_store("HTTPS://hist.example"), HttpRunStore)  # not a local directory
    with pytest.raises(RunStoreError, match="over plain http"):
        open_store("http://hist.example")  # the token from the environment is not sent in clear
    monkeypatch.setenv(API_TOKEN_ENV, "")
    assert isinstance(open_store("http://hist.example"), HttpRunStore)  # empty means none
