from __future__ import annotations

import pytest

from hermetic.generate.validate import SourceRejected, validate_source
from tests.fakes import good_source


def test_a_marked_test_is_accepted() -> None:
    validate_source(good_source("op"), "op")
    validate_source(
        "import json\nimport pytest\nfrom typing import Any\n\n"
        '@pytest.mark.operation("op")\nasync def test_a(): ...\n',
        "op",
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def test_a(:\n", "not valid Python"),
        ("\x00", "not valid Python"),
        ("import os\n", "importing `os`"),
        ("import httpx\n", "importing `httpx`"),
        ("from typing import sys\n", "importing `sys`"),
        ("from pytest import importorskip\n", "importing `importorskip`"),
        ("__builtins__['x']\n", "`__builtins__` is not allowed"),
        ("getattr(1, 'x')\n", "`getattr` is not allowed"),
        ("import typing\ntyping.sys.modules\n", "attribute `(sys|modules)`"),
        ("import pytest\npytest.importorskip('os')\n", "attribute `importorskip`"),
        ("def test_a(tmp_path): ...\n", "fixture `tmp_path`"),
        ("from subprocess import run\n", "importing `subprocess`"),
        ("import os.path\n", "importing `os.path`"),
        ("from . import x\n", "relative imports"),
        ("from .x import y\n", "relative imports"),
        ("eval('1')\n", "`eval` is not allowed"),
        ("x = open\n", "`open` is not allowed"),
        ("x = ().__class__\n", "attribute `__class__`"),
        ("x = 1\n", "no top-level test_ function"),
        ("import pytest\ndef test_a(): ...\n", "lacks @pytest.mark.operation"),
        (
            'import pytest\n@pytest.mark.operation("other")\ndef test_a(): ...\n',
            "lacks @pytest.mark.operation",
        ),
        (
            'import pytest\n@pytest.mark.operation("op", "x")\ndef test_a(): ...\n',
            "lacks @pytest.mark.operation",
        ),
        (
            "import pytest\n@pytest.mark.operation(name)\ndef test_a(): ...\n",
            "lacks @pytest.mark.operation",
        ),
        (
            "import pytest\n@pytest.mark.slow\ndef test_a(): ...\n",
            "lacks @pytest.mark.operation",
        ),
    ],
)
def test_bad_source_is_rejected(source: str, message: str) -> None:
    with pytest.raises(SourceRejected, match=message):
        validate_source(source, "op")


def test_oversized_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hermetic.generate.validate.MAX_SOURCE_BYTES", 10)
    with pytest.raises(SourceRejected, match="larger than"):
        validate_source(good_source("op"), "op")
