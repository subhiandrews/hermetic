import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hermetic.ingest import IngestError
from hermetic.ingest import docs as docs_module
from hermetic.ingest.docs import MAX_DOC_BYTES, load_docs


def test_unreadable_doc_is_a_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.md").write_text("x")

    def denied(self: Path, *args: Any, **kwargs: Any) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(IngestError, match=r"cannot read doc a\.md"):
        load_docs(tmp_path)


def test_unlistable_directory_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def walk(top: Any, onerror: Any = None, **kwargs: Any) -> Iterator[Any]:
        # what os.walk does when scandir fails: report it through onerror
        onerror(PermissionError(13, "Permission denied", str(tmp_path / "locked")))
        return iter(())

    monkeypatch.setattr(os, "walk", walk)
    with pytest.raises(IngestError, match=r"cannot read docs directory .*locked"):
        load_docs(tmp_path)


def test_too_many_documents_stops_the_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docs_module, "MAX_DOC_FILES", 3)
    for i in range(5):
        (tmp_path / f"{i}.md").write_text("x")
    with pytest.raises(IngestError, match="more than 3 documents"):
        load_docs(tmp_path)


def test_total_size_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docs_module, "MAX_TOTAL_DOC_BYTES", 10)
    for i in range(3):
        (tmp_path / f"{i}.md").write_text("123456")  # each is under the per-file cap
    with pytest.raises(IngestError, match="in total"):
        load_docs(tmp_path)


def test_doc_removed_during_the_walk_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docs_module, "_candidates", lambda root: [root / "gone.md"])
    with pytest.raises(IngestError, match=r"cannot read doc gone\.md"):
        load_docs(tmp_path)


def test_hidden_directories_are_never_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "x.md").write_text("never read")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text("a")
    visited: list[str] = []
    real_walk = os.walk

    def spy(top: Any, **kwargs: Any) -> Iterator[Any]:
        for entry in real_walk(top, **kwargs):
            visited.append(entry[0])
            yield entry

    monkeypatch.setattr(os, "walk", spy)
    assert [d.path for d in load_docs(tmp_path)] == ["sub/a.md"]
    assert not any(".venv" in v for v in visited)  # pruned, not walked and filtered


def test_symlinked_directories_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("elsewhere")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "linked").symlink_to(outside, target_is_directory=True)
    (docs / "ok.md").write_text("ok")
    assert [d.path for d in load_docs(docs)] == ["ok.md"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
def test_special_files_are_skipped_not_read(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.md")  # reading it would block forever
    (tmp_path / "ok.md").write_text("ok")
    assert [d.path for d in load_docs(tmp_path)] == ["ok.md"]


def test_directories_sort_before_files_with_the_same_prefix(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.md").write_text("nested")
    (tmp_path / "a.md").write_text("flat")
    assert [d.path for d in load_docs(tmp_path)] == ["a/b.md", "a.md"]


def test_loads_recognised_docs_sorted_with_kinds(tmp_path: Path) -> None:
    (tmp_path / "adr").mkdir()
    (tmp_path / "adr" / "0001.md").write_text("# decision")
    (tmp_path / "flow.mmd").write_text("graph TD; A-->B")
    (tmp_path / "notes.TXT").write_text("plain")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    docs = load_docs(tmp_path)
    assert [(d.path, d.kind) for d in docs] == [
        ("adr/0001.md", "markdown"),
        ("flow.mmd", "diagram"),
        ("notes.TXT", "text"),
    ]
    assert docs[0].text == "# decision"
    assert len(docs[0].sha256) == 64


def test_hash_tracks_content(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("one")
    first = load_docs(tmp_path)[0].sha256
    path.write_text("two")
    assert load_docs(tmp_path)[0].sha256 != first


def test_skips_hidden_and_symlinked_files(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.md").write_text("hidden dir")
    (tmp_path / ".secret.md").write_text("hidden file")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.md").write_text("elsewhere")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "link.md").symlink_to(outside / "real.md")
    (docs / "ok.md").write_text("ok")
    assert [d.path for d in load_docs(docs)] == ["ok.md"]


def test_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not a directory"):
        load_docs(tmp_path / "nope")


def test_oversized_doc_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * (MAX_DOC_BYTES + 1))
    with pytest.raises(IngestError, match="larger than"):
        load_docs(tmp_path)


def test_non_utf8_doc_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(IngestError, match="not valid UTF-8"):
        load_docs(tmp_path)
