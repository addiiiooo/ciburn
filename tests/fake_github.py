"""An in-memory fake of the GitHub REST endpoints ciburn uses, served through httpx.MockTransport."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from conftest import load_api_fixture


@dataclass
class FakeGitHub:
    repo: dict[str, Any]
    workflows: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    jobs: dict[int, list[dict[str, Any]]]
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    tree: list[str] = field(default_factory=list)
    workflow_dir: list[dict[str, Any]] = field(default_factory=list)
    raw_files: dict[str, str] = field(default_factory=dict)
    timing: dict[int, dict[str, Any]] = field(default_factory=dict)
    per_page_cap: int = 100
    requests: list[str] = field(default_factory=list)
    fail_next: list[tuple[int, dict[str, str], str]] = field(default_factory=list)
    remaining: int = 4999

    @classmethod
    def from_fixtures(cls) -> FakeGitHub:
        runs = load_api_fixture("runs_pallets_flask.json")["workflow_runs"]
        jobs = load_api_fixture("jobs_pallets_flask.json")["jobs"]
        wfs = load_api_fixture("workflows_astral_ruff.json")["workflows"]
        repo = {
            "id": 596892,
            "full_name": "pallets/flask",
            "private": False,
            "default_branch": "main",
            "language": "Python",
            "stargazers_count": 70000,
            "pushed_at": "2026-09-11T00:00:00Z",
        }
        return cls(repo=repo, workflows=wfs, runs=runs, jobs={int(runs[0]["id"]): jobs})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- request handling ----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append(path + ("?" + url.query if url.query else ""))
        if self.fail_next:
            status, headers, body = self.fail_next.pop(0)
            return httpx.Response(status, headers=headers, content=body.encode())
        self.remaining -= 1
        headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": str(self.remaining),
            "x-ratelimit-reset": "1789153245",
            "x-github-api-version-selected": "2022-11-28",
        }
        if path == "/rate_limit":
            return self._json(
                {"resources": {"core": {"limit": 5000, "remaining": self.remaining}}}, headers
            )
        if url.netloc == "raw.example.com":
            text = self.raw_files.get(path.lstrip("/"))
            if text is None:
                return httpx.Response(404, content=b"not found")
            return httpx.Response(200, content=text.encode(), headers=headers)
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not m:
            return self._json({"message": "Not Found"}, headers, 404)
        rest = m.group(3) or ""
        if f"{m.group(1)}/{m.group(2)}".lower() != str(self.repo["full_name"]).lower():
            return self._json({"message": "Not Found"}, headers, 404)
        if rest == "":
            return self._json(self.repo, headers)
        if rest == "/actions/workflows":
            return self._paged(self.workflows, "workflows", qs, request, headers)
        if rest == "/actions/runs":
            runs = self.runs
            created = qs.get("created", "")
            if created.startswith(">="):
                runs = [r for r in runs if r["created_at"][:10] >= created[2:]]
            return self._paged(runs, "workflow_runs", qs, request, headers)
        jm = re.match(r"^/actions/runs/(\d+)/jobs$", rest)
        if jm:
            rid = int(jm.group(1))
            if rid not in self.jobs:
                return self._json({"message": "Not Found"}, headers, 404)
            return self._paged(self.jobs[rid], "jobs", qs, request, headers)
        tm = re.match(r"^/actions/runs/(\d+)/timing$", rest)
        if tm:
            return self._json(self.timing.get(int(tm.group(1)), {"billable": {}}), headers)
        cm = re.match(r"^/commits/([0-9a-f]+)$", rest)
        if cm:
            c = self.commits.get(cm.group(1))
            if c is None:
                return self._json({"message": "Not Found"}, headers, 404)
            return self._json(c, headers)
        if rest.startswith("/git/trees/"):
            return self._json(
                {"tree": [{"path": p, "type": "blob"} for p in self.tree], "truncated": False},
                headers,
            )
        if rest == "/contents/.github/workflows":
            if not self.workflow_dir:
                return self._json({"message": "Not Found"}, headers, 404)
            return self._json(self.workflow_dir, headers)
        fm = re.match(r"^/contents/(.+)$", rest)
        if fm:
            text = self.raw_files.get(fm.group(1))
            if text is None:
                return self._json({"message": "Not Found"}, headers, 404)
            import base64

            return self._json(
                {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}, headers
            )
        return self._json({"message": "Not Found"}, headers, 404)

    def _paged(
        self,
        items: list[Any],
        key: str,
        qs: dict[str, str],
        request: httpx.Request,
        headers: dict[str, str],
    ) -> httpx.Response:
        per_page = min(int(qs.get("per_page", "30")), self.per_page_cap)
        page = int(qs.get("page", "1"))
        start = (page - 1) * per_page
        chunk = items[start : start + per_page]
        if start + per_page < len(items):
            nxt = request.url.copy_set_param("page", str(page + 1))
            headers = {**headers, "Link": f'<{nxt}>; rel="next"'}
        return self._json({"total_count": len(items), key: chunk}, headers)

    @staticmethod
    def _json(body: Any, headers: dict[str, str], status: int = 200) -> httpx.Response:
        return httpx.Response(
            status,
            content=json.dumps(body).encode(),
            headers={**headers, "content-type": "application/json"},
        )
