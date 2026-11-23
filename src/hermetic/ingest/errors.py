"""Errors raised by the ingest stage."""


class IngestError(Exception):
    """The spec or docs could not be read or are not usable. The message is user-facing."""
