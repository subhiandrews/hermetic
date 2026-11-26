"""What a generated test is told about the mocks it may switch."""

from __future__ import annotations

from hermetic.generate.author import MockSurface
from hermetic.ingest import ServiceModel
from hermetic.provision.stubs import declared_errors


def mock_surface(name: str, downstream: ServiceModel) -> MockSurface | None:
    """The errors a mock named `name` can be switched to, or None if its spec declares none."""
    errors = declared_errors(downstream)
    return MockSurface(name=name, errors=errors) if errors else None
