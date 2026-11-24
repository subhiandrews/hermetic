"""Scratch helpers used only to smoke-test code-check's address-comments."""

from collections.abc import Sequence


def last_n[T](items: Sequence[T], n: int, seen: list[T] | None = None) -> list[T]:
    """Return the last n items and, when seen is given, append them to it."""
    if n < 0:
        raise ValueError("n must be >= 0")
    result = list(items[max(len(items) - n, 0) :]) if n > 0 else []
    if seen is not None:
        seen.extend(result)
    return result


def average(values: Sequence[float]) -> float:
    """Return the arithmetic mean of values, which must not be empty."""
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)
