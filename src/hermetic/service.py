"""Service ids: the name a microservice's runs and tests are grouped under."""

from __future__ import annotations

import re

# A service id becomes a directory name (history and tests) and a URL segment, so it must not
# be able to name a path. Lowercase letters, digits and hyphens, starting with a letter or digit.
SERVICE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
DEFAULT_SERVICE = "default"  # where runs recorded before services existed are filed
_SERVICE = re.compile(SERVICE_PATTERN)
_MAX_LENGTH = 63


def is_valid_service(service: str) -> bool:
    return _SERVICE.fullmatch(service) is not None


def slugify(title: str) -> str:
    """A service id from a spec title: "Conduit API (v2)" becomes "conduit-api-v2". Empty when the
    title has no ASCII letters or digits to make one from."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:_MAX_LENGTH].rstrip("-")


class InvalidServiceError(ValueError):
    """The name given for a service cannot be used as one."""


def resolve_service(title: str | None, override: str | None) -> str:
    """The id to file a run under: `override` if given, else a slug of the spec title."""
    if override is not None:
        if not is_valid_service(override):
            raise InvalidServiceError(
                f"invalid service {override!r}: use lowercase letters, digits and hyphens, "
                "at most 63, starting with a letter or digit"
            )
        return override
    if title is None:
        raise InvalidServiceError("name the service with --service, or give --spec to derive it")
    slug = slugify(title)
    if not slug:  # a shared fallback id would silently merge unrelated services
        raise InvalidServiceError(
            f"cannot derive a service id from the title {title!r}: use --service"
        )
    return slug
