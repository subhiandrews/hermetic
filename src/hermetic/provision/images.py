"""The images Hermetic is willing to run, and how each kind of dependency is served.

Provisioning talks to the host's Docker daemon, which is root-equivalent, so what may run is a
short list kept in code, not whatever a document or an agent names. Adding an engine means
adding it here, in a pull request.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermetic.discover.models import DependencyKind


@dataclass(frozen=True)
class Engine:
    image: str  # a full reference with a pinned tag
    port: int  # the port the engine listens on inside its container
    # What the container may use, tmpfs data included. Docker kills what goes over it, so it has
    # to cover the engine's start-up peak: MySQL's first-run server alone reaches about 660 MiB,
    # and Mongo needs room for its health check (`mongosh`, a second process) beside the server.
    memory: str = "512m"


ENGINES: dict[DependencyKind, Engine] = {
    "postgres": Engine("postgres:16-alpine", 5432),
    "mysql": Engine("mysql:8.4", 3306, memory="1g"),
    "redis": Engine("redis:7-alpine", 6379),
    "mongo": Engine("mongo:7", 27017, memory="1g"),
    "http": Engine("wiremock/wiremock:3.9.1", 8080),
}
APPROVED_IMAGES = frozenset(engine.image for engine in ENGINES.values())
