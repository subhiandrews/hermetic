"""Read-only web UI over the run history."""

from hermetic.web.app import LOOPBACK_HOSTS, WILDCARD, create_app

__all__ = ["LOOPBACK_HOSTS", "WILDCARD", "create_app"]
