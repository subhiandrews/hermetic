from __future__ import annotations

import pytest

from hermetic.generate.validate import SourceRejected, validate_source
from tests.fakes import good_source


def test_a_marked_test_is_accepted() -> None:
    validate_source(good_source("op"), "op")
    validate_source(
        'import json\nimport pytest\n\n@pytest.mark.operation("op")\nasync def test_a(): ...\n',
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
        "import json\nimport pytest\nimport re\nimport uuid\n"
        "from pytest import approx\n\n"
        '@pytest.mark.operation("op")\n'
        "@pytest.mark.filterwarnings('ignore')\n"
        "def test_a(hermetic_client):\n"
        "    system, main, operator, types = 1, 2, 3, 4  # ordinary variables are fine\n"
        "    assert 1.0 == approx(1.0) and '{:.2f} {name!r} {{x}}'.format(1.5, name=1)\n"
        "    assert json.loads('1') == 1 and re.compile('x') and uuid.uuid4()\n"
        "    if not hermetic_client:\n"
        "        pytest.skip('not on this server')  # skipping happens in the body\n"
        "    body = '{\"__proto__\": 1}'  # plain JSON-looking text is not a format string\n"
        "    with pytest.raises(ValueError):\n"
        "        '{'.format()  # not a valid format string: fails at run time, not here\n"
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
        ("from json import sys\n", "importing `sys`"),
        ("import typing\n", "importing `typing`"),
        ("from typing import evaluate_forward_ref\n", "importing `typing`"),
        ("from pytest import importorskip\n", "importing `importorskip`"),
        ("__builtins__['x']\n", "`__builtins__` is not allowed"),
        ("getattr(1, 'x')\n", "`getattr` is not allowed"),
        ("import json\njson.sys.modules\n", "attribute `(sys|modules)`"),
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
        ("import json\njson.get_type_hints(1)\n", "attribute `get_type_hints`"),
        ("import json\njson.ForwardRef('os')\n", "attribute `ForwardRef`"),
        ("import string\nstring.Formatter()\n", "attribute `Formatter`"),
        ("from string import Formatter\n", "importing `Formatter`"),
        (
            "import pytest\n@pytest.mark.skipif(\"os.system('x')\")\ndef test_a(): ...\n",
            "attribute `skipif`",
        ),
        (
            "import pytest\n@pytest.mark.xfail(condition='1')\ndef test_a(): ...\n",
            "attribute `xfail`",
        ),
        (  # bound to a name first, then called with a string condition
            "import pytest\nm = pytest.mark.skipif(False, reason='r')\n"
            "@m(\"os.system('id')\")\ndef test_a(): ...\n",
            "attribute `skipif`",
        ),
        (
            "import pytest\n@pytest.mark.skipif(False, reason='r')\ndef test_a(): ...\n",
            "attribute `skipif`",
        ),
        ("x = '{0.__class__}'.format(1)\n", "format field may not use attribute or index"),
        ("(x for x in []).gi_frame.f_globals\n", "attribute `f_globals`"),
        ("def f(g):\n    return g.gi_frame\n", "attribute `gi_frame`"),
        ("pytest_plugins = ['pytester']\n", "`pytest_plugins` is not allowed"),
        ("def test_a(pytester): ...\n", "fixture `pytester`"),
        ("from json import *\n", "importing `*`"),
        ("from pytest import mark\n@mark.skipif('1')\ndef test_a(): ...\n", "attribute `skipif`"),
        ("import pytest\nm = pytest.mark.skipif\n", "attribute `skipif`"),
        (
            "import pytest\nc = '1'\n@pytest.mark.skipif(c)\ndef test_a(): ...\n",
            "attribute `skipif`",
        ),
        ("from pytest.mark import skipif\n", "importing `pytest.mark`"),
        ("from pytest import mark\nmark.skipif('1')\n", "skipif"),
        (
            "import pytest\n@pytest.mark.skipif(False)(\"os.system('id')\")\ndef test_a(): ...\n",
            "attribute `skipif`",
        ),
        (
            "import pytest\nm = pytest.mark.skipif(False).with_args('x')\n",
            "attribute `with_args`",
        ),
        (
            "import pytest\nwith pytest.raises(ZeroDivisionError) as e:\n    1 / 0\n"
            "e.traceback[0].frame.eval('1')\n",
            "attribute `eval`",
        ),
        ("def f(e):\n    return e.frame.eval('1')\n", "attribute `eval`"),
        ("def f(e):\n    return e.exec('1')\n", "attribute `exec`"),
        ("skipif\n", "`skipif` is not allowed"),
        ("import uuid\nuuid._get_command_stdout('sh')\n", "attribute `_get_command_stdout`"),
        ("from uuid import _get_command_stdout\n", "importing `_get_command_stdout`"),
        ("import json\njson.operator.attrgetter('x')\n", "attribute `(operator|attrgetter)`"),
        (
            "def f(x):\n    match x:\n        case object(__globals__=g): ...\n",
            "private attributes",
        ),
        ("f = ('{0._' + '_init__}').format\n", "attribute `format`"),
        ("f = str.format\n", "attribute `format`"),
        ("help('modules')\n", "`help` is not allowed"),
        ("import pytest\n@pytest.mark.usefixtures('tmp_path')\ndef test_a(): ...\n", "usefixtures"),
        ("from pytest import usefixtures\n", "importing `usefixtures`"),
        ("@usefixtures('tmp_path')\ndef test_a(): ...\n", "`usefixtures` is not allowed"),
        ("import json\njson.operator\n", "attribute `operator`"),
        ("import pytest\npytest.MonkeyPatch()\n", "attribute `MonkeyPatch`"),
        ("import pytest\npytest.main([])\n", "attribute `main`"),
        ("import pytest\npytest.console_main()\n", "attribute `console_main`"),
        (
            "import pytest\npytest.PytestPluginManager().import_plugin('subprocess')\n",
            "attribute `PytestPluginManager`",
        ),
        ("import pytest\npytest.TempPathFactory\n", "attribute `TempPathFactory`"),
        ("from pytest import PytestPluginManager\n", "importing `PytestPluginManager`"),
        ("import pytest as p\n", "may not be imported under another name"),
        ("import pytest\np = pytest\n", "`pytest` is not allowed"),
        ("import json.tool\n", "importing `json.tool`"),
        ("from json import tool\n", "importing `tool`"),
        ("import json\njson.tool\n", "submodule `tool`"),
        ("import json as j\nj.tool.argparse\n", "submodule `tool`"),
        (
            "def test_a():\n    match 1:\n        case object(sys=s): ...\n",
            "class patterns may not bind forbidden or private attributes",
        ),
        (
            "def test_a():\n    match 1:\n        case object(format=f): ...\n",
            "class patterns may not bind forbidden or private attributes",
        ),
        ("import json\njson.dumps(1, _ispytest=True)\n", "keyword argument `_ispytest`"),
        (
            "x = '{0.sys.modules[os].environ}'.format(1)\n",
            "format field may not use attribute or index access",
        ),
        ("x = '{0[a]}'.format({})\n", "format field may not use attribute or index access"),
        ("x = '{:{0.x}}'.format(1)\n", "format field may not use attribute or index access"),
        ("import json\njson.codecs.open('x', 'w')\n", "attribute `(codecs|open)`"),
        ("import json\njson.codecs\n", "attribute `codecs`"),
        ("from json import codecs\n", "importing `codecs`"),
        (
            "import pytest\n"
            "@pytest.mark.skipif(**{'condition': \"os.system('id')\"})\n"
            "def test_a(): ...\n",
            "attribute `skipif`",
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


def test_validation_never_imports_a_module_the_test_merely_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *args: asked.append(name))
    # The attribute is reached before the (nested) import that would reject `os`.
    with pytest.raises(SourceRejected, match="importing `os`"):
        validate_source("x = os.path\ndef f():\n    import os\n", "op")
    assert asked == []


def test_oversized_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hermetic.generate.validate.MAX_SOURCE_BYTES", 10)
    with pytest.raises(SourceRejected, match="larger than"):
        validate_source(good_source("op"), "op")
