"""The PyPI README is the repo README with its `main` links rewritten to the release tag.

The rewrite is a `substitutions` entry in pyproject.toml, applied by hatch-fancy-pypi-readme at
build time. These tests apply that same entry to the README so a link form the pattern misses
fails here, not after a release.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO_MAIN_LINK = re.compile(r"github\.com/subhiandrews/hermetic/\w+/main\b")


def _substitution() -> tuple[str, str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    hook = config["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    (entry,) = hook["substitutions"]
    return entry["pattern"], entry["replacement"]


def test_readme_has_links_to_rewrite() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert REPO_MAIN_LINK.search(readme)


def test_every_main_link_is_rewritten_to_the_release_tag() -> None:
    pattern, replacement = _substitution()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shipped = re.sub(pattern, replacement.replace("$HFPR_VERSION", "9.9.9"), readme)
    assert not REPO_MAIN_LINK.search(shipped)
    assert "/blob/v9.9.9/docs/architecture.md" in shipped
    assert "/tree/v9.9.9/examples/petclinic" in shipped


def test_version_placeholder_is_the_one_the_plugin_expands() -> None:
    _, replacement = _substitution()
    assert "$HFPR_VERSION" in replacement


def _constraint_blocks() -> dict[str, str]:
    """Map each pinned package in build-constraints.txt to its text (the pin plus its hashes)."""
    text = (ROOT / "build-constraints.txt").read_text(encoding="utf-8")
    blocks = re.findall(r"^([A-Za-z0-9._-]+)==(\S+) \\\n((?:\s+--hash=\S+(?: \\)?\n)+)", text, re.M)
    return {name.lower(): f"{version}\n{hashes}" for name, version, hashes in blocks}


def test_build_requirements_are_pinned_by_hash() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = _constraint_blocks()
    assert pinned, "build-constraints.txt has no hash-pinned packages"
    for requirement in config["build-system"]["requires"]:
        name, _, version = requirement.partition("==")
        assert name.lower() in pinned, f"{name} is not pinned in build-constraints.txt"
        if version:
            assert pinned[name.lower()].startswith(f"{version}\n"), f"{name} pin differs"


def test_every_pinned_build_package_has_a_hash() -> None:
    text = (ROOT / "build-constraints.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^[A-Za-z0-9._-]+==\S+", text, re.M)
    assert len(pins) == len(_constraint_blocks())
