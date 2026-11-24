"""A fake suite author: canned tests, no API calls."""

from __future__ import annotations

from pathlib import Path

from hermetic.discover.infer import InferRequest, InferResult, Proposal, Quote
from hermetic.generate import AuthoredTest, AuthorRequest

CONDUIT = Path(__file__).parent / "fixtures" / "conduit"


def good_source(operation_id: str, status: int = 200) -> str:
    return (
        "import pytest\n\n\n"
        f'@pytest.mark.operation("{operation_id}")\n'
        "def test_it(hermetic_client):\n"
        f"    assert hermetic_client.get('/ok').status_code == {status}\n"
    )


class FakeAuthor:
    def __init__(self, version: str = "1", note: str = "covers the success case") -> None:
        self._version = version
        self.note = note
        self.requests: list[AuthorRequest] = []
        self.source_for: dict[str, str] = {}
        self.fail_on: str | None = None

    @property
    def prompt_version(self) -> str:
        return self._version

    def write_test(self, request: AuthorRequest) -> AuthoredTest:
        self.requests.append(request)
        operation_id = request.operation.operation_id
        if operation_id == self.fail_on:
            raise RuntimeError("the agent is unavailable")
        source = self.source_for.get(operation_id, good_source(operation_id))
        return AuthoredTest(source=source, note=self.note)


class MailInferrer:
    """What the agent says about the Conduit design note: the one service the hints cannot find."""

    prompt_version = "golden"

    def infer(self, request: InferRequest) -> InferResult:
        return InferResult(
            [
                Proposal(
                    name="mail",
                    kind="http",
                    quotes=[Quote(source="architecture.md", quote="it calls the Mail service")],
                )
            ]
        )
