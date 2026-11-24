from __future__ import annotations

import pytest

from hermetic.service import (
    InvalidServiceError,
    is_valid_service,
    resolve_service,
    slugify,
)


@pytest.mark.parametrize("service", ["conduit", "a", "pet-store-2", "0abc", "a" * 63])
def test_valid_service_ids(service: str) -> None:
    assert is_valid_service(service)


@pytest.mark.parametrize(
    "service", ["", "-a", "A", "a_b", "a/b", "..", "a.b", "a b", "a" * 64, "a\n", "é"]
)
def test_invalid_service_ids(service: str) -> None:
    assert not is_valid_service(service)


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Conduit API (v2)", "conduit-api-v2"),
        ("  Swagger Petstore - OpenAPI 3.0  ", "swagger-petstore-openapi-3-0"),
        ("a" * 100, "a" * 63),
        ("a" * 62 + " b", "a" * 62),  # cut where it would leave a trailing hyphen
    ],
)
def test_slugify_gives_a_valid_id(title: str, slug: str) -> None:
    assert slugify(title) == slug and is_valid_service(slug)


@pytest.mark.parametrize("title", ["???", "", "日本語", " - "])
def test_a_title_with_nothing_to_make_an_id_from_gives_none(title: str) -> None:
    assert slugify(title) == ""
    with pytest.raises(InvalidServiceError, match="use --service"):
        resolve_service(title, None)
    assert resolve_service(title, "chosen-name") == "chosen-name"


def test_an_explicit_service_wins_over_the_title() -> None:
    assert resolve_service("Conduit API", "my-service") == "my-service"


def test_the_title_is_slugified_when_no_service_is_given() -> None:
    assert resolve_service("Conduit API", None) == "conduit-api"


def test_a_bad_explicit_service_is_refused_not_slugified() -> None:
    with pytest.raises(InvalidServiceError, match="invalid service 'Bad Name'"):
        resolve_service("Conduit API", "Bad Name")


def test_without_a_title_or_service_there_is_nothing_to_use() -> None:
    with pytest.raises(InvalidServiceError, match="--service"):
        resolve_service(None, None)
