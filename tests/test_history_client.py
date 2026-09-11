from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from ciburn.history.client import (
    USER_AGENT,
    AuthError,
    GitHubClient,
    GitHubError,
    NotFoundError,
    RateLimitExhaustedError,
    token_from_env,
)
from fake_github import FakeGitHub


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


def make_client(fake: FakeGitHub, **kw: Any) -> tuple[GitHubClient, Sleeper]:
    sl = Sleeper()
    c = GitHubClient("tok", transport=fake.transport(), sleep=sl, clock=lambda: 1789153200.0, **kw)
    return c, sl


def test_headers_and_auth() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, json={"ok": True})

    c = GitHubClient("abc", transport=httpx.MockTransport(handler))
    assert c.get_json("/rate_limit") == {"ok": True}
    assert seen["authorization"] == "Bearer abc"
    assert seen["user-agent"] == USER_AGENT
    assert seen["x-github-api-version"] == "2022-11-28"
    assert c.authenticated
    c.close()
    c2 = GitHubClient("", transport=httpx.MockTransport(handler))
    assert not c2.authenticated


def test_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert token_from_env() is None
    monkeypatch.setenv("GH_TOKEN", " x ")
    assert token_from_env() == "x"
    with GitHubClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
        assert c.token == "x"
    assert os.environ["GH_TOKEN"] == " x "


def test_pagination_and_rate_state() -> None:
    fake = FakeGitHub.from_fixtures()
    fake.per_page_cap = 2
    c, _ = make_client(fake)
    runs = list(c.list_runs("pallets", "flask"))
    assert len(runs) == 5
    assert c.rate.remaining is not None
    assert c.rate.requests_made == 3
    assert [r for r in fake.requests if "page=" in r]
    limited = list(c.list_runs("pallets", "flask", max_pages=1))
    assert len(limited) == 2
    assert c.list_workflows("pallets", "flask")
    jobs = c.list_jobs("pallets", "flask", int(runs[0]["id"]))
    assert jobs[0]["name"] == "main"
    assert "filter=all" in fake.requests[-1]


def test_errors() -> None:
    fake = FakeGitHub.from_fixtures()
    c, _ = make_client(fake)
    with pytest.raises(NotFoundError):
        c.get_repo("nope", "nope")
    fake.fail_next.append((401, {}, '{"message":"Bad credentials"}'))
    with pytest.raises(AuthError, match="Bad credentials"):
        c.get_repo("pallets", "flask")
    fake.fail_next.append((422, {}, "plain text error"))
    with pytest.raises(GitHubError, match="plain text"):
        c.get_repo("pallets", "flask")
    fake.fail_next.append((403, {}, '{"message":"Resource not accessible by integration"}'))
    with pytest.raises(GitHubError, match="not accessible"):
        c.get_repo("pallets", "flask")


def test_primary_rate_limit_sleeps_until_reset() -> None:
    fake = FakeGitHub.from_fixtures()
    c, sl = make_client(fake)
    fake.fail_next.append(
        (
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1789153230"},
            '{"message":"API rate limit exceeded"}',
        )
    )
    assert c.get_repo("pallets", "flask")["full_name"] == "pallets/flask"
    assert sl.calls == [32.0]  # reset - now + 2
    assert c.rate.seconds_slept == 32.0


def test_retry_after_and_secondary_limit() -> None:
    fake = FakeGitHub.from_fixtures()
    c, sl = make_client(fake)
    fake.fail_next.append((429, {"retry-after": "7"}, '{"message":"slow down"}'))
    fake.fail_next.append((403, {}, '{"message":"You have exceeded a secondary rate limit"}'))
    assert c.get_repo("pallets", "flask")
    assert sl.calls == [8.0, 120.0]


def test_rate_limit_exhausted_after_retries() -> None:
    fake = FakeGitHub.from_fixtures()
    c, sl = make_client(fake, max_retries=2)
    for _ in range(3):
        fake.fail_next.append((429, {"retry-after": "1"}, "{}"))
    with pytest.raises(RateLimitExhaustedError):
        c.get_repo("pallets", "flask")
    assert len(sl.calls) == 2


def test_server_errors_and_network_errors_retry() -> None:
    fake = FakeGitHub.from_fixtures()
    c, sl = make_client(fake)
    fake.fail_next.append((502, {}, "bad gateway"))
    fake.fail_next.append((503, {}, "unavailable"))
    assert c.get_repo("pallets", "flask")
    assert sl.calls == [1.0, 2.0]
    c2, _ = make_client(fake, max_retries=1)
    fake.fail_next.append((500, {}, "x"))
    fake.fail_next.append((500, {}, "x"))
    with pytest.raises(GitHubError, match="x"):
        c2.get_repo("pallets", "flask")

    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"id": 1})

    sl2 = Sleeper()
    c3 = GitHubClient("t", transport=httpx.MockTransport(flaky), sleep=sl2)
    assert c3.get_json("/x") == {"id": 1}
    assert sl2.calls == [1.0, 2.0]
    sl3 = Sleeper()
    c4 = GitHubClient("t", transport=httpx.MockTransport(flaky), sleep=sl3, max_retries=0)
    calls["n"] = 0
    with pytest.raises(GitHubError, match="network error"):
        c4.get_json("/x")


def test_tree_contents_and_raw_download() -> None:
    fake = FakeGitHub.from_fixtures()
    fake.tree = ["README.md", "src/a.py", "docs/index.md"]
    fake.raw_files[".github/workflows/ci.yml"] = "on: push\njobs: {}\n"
    c, _ = make_client(fake)
    paths, truncated = c.get_tree_paths("pallets", "flask", "main")
    assert paths == fake.tree
    assert truncated is False
    assert c.list_workflow_dir("pallets", "flask", "main") == []
    fake.workflow_dir = [
        {
            "type": "file",
            "path": ".github/workflows/ci.yml",
            "sha": "abc",
            "download_url": "https://raw.example.com/.github/workflows/ci.yml",
        }
    ]
    assert len(c.list_workflow_dir("pallets", "flask", "main")) == 1
    assert (
        c.get_file_text("pallets", "flask", ".github/workflows/ci.yml", "main")
        == "on: push\njobs: {}\n"
    )
    assert (
        c.download_text("https://raw.example.com/.github/workflows/ci.yml")
        == "on: push\njobs: {}\n"
    )
    with pytest.raises(NotFoundError):
        c.get_file_text("pallets", "flask", "missing.yml", "main")
    assert c.get_rate_limit()["resources"]["core"]["limit"] == 5000
    assert c.get_run_timing("pallets", "flask", 1) == {"billable": {}}


def test_get_file_text_download_url_fallback() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "contents" in str(req.url):
            return httpx.Response(200, json={"download_url": "https://raw.example.com/f.yml"})
        return httpx.Response(200, content=b"on: push\n")

    c = GitHubClient("t", transport=httpx.MockTransport(handler))
    assert c.get_file_text("o", "r", "f.yml", "main") == "on: push\n"
    c2 = GitHubClient(
        "t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"weird": 1}))
    )
    with pytest.raises(GitHubError, match="unexpected contents"):
        c2.get_file_text("o", "r", "f.yml", "main")
