"""Reporters: terminal (rich), markdown, json, html."""

from __future__ import annotations

from ciburn.report.html import render_html
from ciburn.report.jsonfmt import render_json
from ciburn.report.markdown import render_markdown
from ciburn.report.terminal import render_terminal

FORMATS = ("terminal", "md", "json", "html")

__all__ = ["FORMATS", "render_html", "render_json", "render_markdown", "render_terminal"]
