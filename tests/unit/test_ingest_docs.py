from pathlib import Path

import pytest

from hermetic.ingest import IngestError
from hermetic.ingest.docs import MAX_DOC_BYTES, load_docs


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
