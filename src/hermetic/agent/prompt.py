"""Helpers that frame document and spec text as data inside a prompt."""

from __future__ import annotations

import json


def data(text: str) -> str:
    """Break up closing tags so embedded text cannot end the block it sits in."""
    return text.replace("</", "<\\/")


def quoted(text: str) -> str:
    """A JSON string literal on one line, so spec text cannot add lines or close a tag."""
    return data(json.dumps(text, ensure_ascii=False))
