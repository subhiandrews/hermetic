"""Render a recorded request as a curl command."""

from __future__ import annotations

import re
import shlex

from hermetic.history.models import Exchange
from hermetic.redact import REDACTED

# Headers curl or the server sets itself; replaying them only adds noise or breaks the repro.
_SKIPPED_HEADERS = frozenset(
    {"host", "content-length", "connection", "accept-encoding", "user-agent", "transfer-encoding"}
)


def _placeholder(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "SECRET"


def build_curl(exchange: Exchange) -> str:
    """A curl command for the exchange's request.

    Redacted header values become `$NAME` placeholders (double-quoted so a shell expands
    them), so a copied command works once the variable is set.
    """
    parts = ["curl", "-sS", "-i", "-X", shlex.quote(exchange.method)]
    for name, value in exchange.request_headers.items():
        if name.lower() in _SKIPPED_HEADERS:
            continue
        if value == REDACTED:
            safe_name = re.sub(r"[^A-Za-z0-9-]", "-", name)
            parts += ["-H", f'"{safe_name}: ${_placeholder(name)}"']
        else:
            parts += ["-H", shlex.quote(f"{name}: {value}")]
    if exchange.request_body is not None:
        parts += ["--data-raw", shlex.quote(exchange.request_body)]
    parts.append(shlex.quote(exchange.url))
    return " ".join(parts)
