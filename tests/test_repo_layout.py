from __future__ import annotations

from pathlib import Path

from ciburn.repo_layout import RepoLayout


def test_from_path(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "j.md").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "CHANGELOG.rst").write_text("x", encoding="utf-8")
    layout = RepoLayout.from_path(tmp_path)
    assert layout.top_level_dirs == {"docs", "src"}
    assert layout.markdown_files == 3
    assert layout.total_files == 4
    assert layout.doc_dirs == ["docs"]
    assert layout.separable_trees == ["docs/**", "**.md"]
    assert RepoLayout.from_path(tmp_path / "missing").separable_trees == []
    small = RepoLayout.from_path(tmp_path, max_files=1)
    assert small.total_files == 1


def test_from_tree() -> None:
    layout = RepoLayout.from_tree(["src/a.py", "website/index.md", "README.md"])
    assert layout.top_level_dirs == {"src", "website"}
    assert layout.markdown_files == 2
    assert layout.separable_trees == ["website/**"]
