"""Which host names mean this machine."""

# As urlsplit's `hostname` and a bind address give them; a `Host` header brackets the IPv6 one.
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


def is_header_token(token: str) -> bool:
    """Whether a token can travel in an Authorization header: printable ASCII, no spaces."""
    return token.isascii() and token.isprintable() and not any(c.isspace() for c in token)
