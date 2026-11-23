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


def test_attribute_names_that_match_forbidden_builtins_are_accepted() -> None:
    validate_source(
        "import re\n\n"
        '@pytest.mark.operation("op")\n'
        "def test_a(hermetic_client):\n"
        "    assert re.compile('x') and hermetic_client.get('/').type\n",
        "op",
    )


def test_ordinary_strings_marks_and_header_edits_are_accepted() -> None:
    validate_source(
        "import pytest\n\n"
        '@pytest.mark.operation("op")\n'
        "@pytest.mark.skipif(False, reason='not on this server')\n"
        "def test_a(hermetic_client):\n"
        "    hermetic_client.headers['X-Test'] = 'a {b} c_d'\n"
        "    assert 'user_{}'.format(1) and '__' not in 'x'\n\n"
        "class TestShapes:\n"
        '    @pytest.mark.operation("op")\n'
        "    def test_b(self, hermetic_client):\n"
        "        for _ in range(2):\n"
        "            assert hermetic_client.get('/ok')\n",
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
        ("def test_a(hermetic_client):\n    hermetic_client.event_hooks = {}\n", "`event_hooks`"),
        ("def test_a(hermetic_client):\n    hermetic_client.base_url = 'x'\n", "`base_url`"),
        ("def test_a(c):\n    c.timeout = 1\n", "assigning to or deleting `.timeout`"),
        ("def test_a(c):\n    c.timeout += 1\n", "assigning to or deleting `.timeout`"),
        ("def test_a(c):\n    del c.timeout\n", "assigning to or deleting `.timeout`"),
        ("import typing\ntyping.get_type_hints(1)\n", "attribute `get_type_hints`"),
        ("import typing\ntyping.ForwardRef('os')\n", "attribute `ForwardRef`"),
        ("import string\nstring.Formatter()\n", "attribute `Formatter`"),
        ("from string import Formatter\n", "importing `Formatter`"),
        (
            "import pytest\n@pytest.mark.skipif(\"os.system('x')\")\ndef test_a(): ...\n",
            "may only take a constant condition",
        ),
        (
            "import pytest\n@pytest.mark.xfail(condition='1')\ndef test_a(): ...\n",
            "may only take a constant condition",
        ),
        ("x = '{0.__class__}'.format(1)\n", "format string may not reach"),
        ("(x for x in []).gi_frame.f_globals\n", "attribute `f_globals`"),
        ("def f(g):\n    return g.gi_frame\n", "attribute `gi_frame`"),
        ("pytest_plugins = ['pytester']\n", "`pytest_plugins` is not allowed"),
        ("def test_a(pytester): ...\n", "fixture `pytester`"),
        ("from typing import *\n", "importing `*`"),
        ("get_type_hints\n", "`get_type_hints` is not allowed"),
        ("from pytest import mark\n@mark.skipif('1')\ndef test_a(): ...\n", "constant condition"),
        ("import pytest\nm = pytest.mark.skipif\n", "attribute `skipif`"),
        (
            "import pytest\nc = '1'\n@pytest.mark.skipif(c)\ndef test_a(): ...\n",
            "constant condition",
        ),
        ("from pytest.mark import skipif\n", "importing `skipif`"),
        ("skipif\n", "`skipif` is not allowed"),
        ("import uuid\nuuid._get_command_stdout('sh')\n", "attribute `_get_command_stdout`"),
        ("from uuid import _get_command_stdout\n", "importing `_get_command_stdout`"),
        ("import typing\ntyping.operator.attrgetter('x')\n", "attribute `(operator|attrgetter)`"),
        ("import typing\ntyping.operator\n", "attribute `operator`"),
        ("import pytest\npytest.MonkeyPatch()\n", "attribute `MonkeyPatch`"),
        ("import pytest\npytest.main([])\n", "attribute `main`"),
        (
            "import pytest\n"
            "@pytest.mark.skipif(**{'condition': \"os.system('id')\"})\n"
            "def test_a(): ...\n",
            "may only take a constant condition",
        ),
        ("x = ('{0._' + '_g}').format(1)\n", "`.format` may only be called on a single string"),
        ("fmt = '{}'\nx = fmt.format(1)\n", "`.format` may only be called on a single string"),
        (
            'import pytest\n@pytest.mark.operation("op")\ndef test_a(): ...\n'
            "class TestX:\n    def test_b(self): ...\n",
            "test_b lacks @pytest.mark.operation",
        ),
        (
            'import pytest\n@pytest.mark.operation("op")\ndef test_a(): ...\n'
            "if True:\n    def test_c(): ...\n",
            "test_c lacks @pytest.mark.operation",
        ),
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
