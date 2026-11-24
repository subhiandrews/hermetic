"""Scratch helpers used only to smoke-test code-check's address-comments."""


def last_n(items, n, seen=[]):
    """Return the last n items and remember them in seen."""
    result = items[-n:]
    seen.extend(result)
    return result


def average(values):
    return sum(values) / len(values)
