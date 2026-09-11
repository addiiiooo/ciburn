"""Facts about a repository's file tree that some rules need (W004)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DOC_DIRS = {"docs", "doc", "documentation", "website", "site"}
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    "target",
    "dist",
    "build",
}


@dataclass
class RepoLayout:
    top_level_dirs: set[str] = field(default_factory=set)
    markdown_files: int = 0
    total_files: int = 0

    @property
    def doc_dirs(self) -> list[str]:
        return sorted(d for d in self.top_level_dirs if d.lower() in DOC_DIRS)

    @property
    def separable_trees(self) -> list[str]:
        """Path patterns that a docs-only change would touch."""
        out = [f"{d}/**" for d in self.doc_dirs]
        if self.markdown_files >= 3:
            out.append("**.md")
        return out

    @classmethod
    def from_path(cls, root: Path, max_files: int = 20000) -> RepoLayout:
        layout = cls()
        if not root.is_dir():
            return layout
        for child in root.iterdir():
            if child.is_dir() and child.name not in SKIP_DIRS:
                layout.top_level_dirs.add(child.name)
        count = 0
        for p in root.rglob("*"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if p.is_file():
                count += 1
                if p.suffix.lower() in (".md", ".mdx", ".rst"):
                    layout.markdown_files += 1
                if count >= max_files:
                    break
        layout.total_files = count
        return layout

    @classmethod
    def from_tree(cls, paths: Iterable[str]) -> RepoLayout:
        layout = cls()
        for path in paths:
            layout.total_files += 1
            parts = path.split("/")
            if len(parts) > 1:
                layout.top_level_dirs.add(parts[0])
            if path.lower().endswith((".md", ".mdx", ".rst")):
                layout.markdown_files += 1
        return layout
