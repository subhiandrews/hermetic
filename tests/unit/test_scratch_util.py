import pytest

from hermetic.scratch_util import average, last_n


def test_last_n_returns_the_tail() -> None:
    assert last_n([1, 2, 3], 2) == [2, 3]


def test_last_n_zero_is_empty_not_everything() -> None:
    assert last_n([1, 2, 3], 0) == []


def test_last_n_larger_than_the_list_returns_all() -> None:
    assert last_n([1, 2], 5) == [1, 2]


def test_last_n_rejects_negative_n() -> None:
    with pytest.raises(ValueError, match="n must be >= 0"):
        last_n([1, 2, 3], -2)


def test_last_n_does_not_share_state_between_calls() -> None:
    last_n([1], 1)
    seen: list[int] = []
    assert last_n([2], 1, seen) == [2]
    assert seen == [2]
    assert last_n([3], 1) == [3]
    assert seen == [2]


def test_average() -> None:
    assert average([1, 2, 3]) == 2


def test_average_rejects_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        average([])
