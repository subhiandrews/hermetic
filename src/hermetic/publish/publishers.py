"""Suite publishers: where a generated suite goes. Third parties add them via entry points."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

ENTRY_POINT_GROUP = "hermetic.publishers"


class PublisherError(Exception):
    """A publisher is unknown, failed to load, or failed to deliver the suite."""


class SuitePublisher(Protocol):
    def publish(self, service: str, suite_dir: Path) -> str:
        """Deliver the suite in `suite_dir` for `service` and say where it went.

        The answer is for people to read: a directory, a URL, a pull request address.
        """
        ...


PublisherFactory = Callable[[], SuitePublisher]


class LocalPublisher:
    """Leaves the suite where it is: the tests are already in the repository they run from."""

    def publish(self, service: str, suite_dir: Path) -> str:
        return str(suite_dir)


def _github_pr() -> SuitePublisher:
    from hermetic.publish.github_pr import GitHubPRPublisher  # loaded only when github-pr is chosen

    return GitHubPRPublisher.from_environment()


BUILTIN_PUBLISHERS: dict[str, PublisherFactory] = {
    "local": LocalPublisher,
    "github-pr": _github_pr,
}


def load_publisher(name: str) -> SuitePublisher:
    """Build the named publisher. Built-ins win; other names resolve through entry points.

    An entry point in the `hermetic.publishers` group must name a PublisherFactory.
    """
    third_party = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
    if name in BUILTIN_PUBLISHERS:
        factory: PublisherFactory = BUILTIN_PUBLISHERS[name]
    elif name in third_party:
        try:
            factory = third_party[name].load()
        except Exception as exc:  # a plugin can fail in any way at import time
            raise PublisherError(f"publisher {name!r} failed to load: {exc}") from exc
    else:
        known = ", ".join(sorted({*BUILTIN_PUBLISHERS, *third_party}))
        raise PublisherError(f"unknown publisher {name!r} (available: {known})")
    try:
        return factory()
    except Exception as exc:  # a third-party factory can fail in any way
        raise PublisherError(f"publisher {name!r} could not be created: {exc}") from exc
