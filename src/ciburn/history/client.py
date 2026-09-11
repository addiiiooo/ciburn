"""A small, polite GitHub REST client.

- authenticated when ``GITHUB_TOKEN``/``GH_TOKEN`` (or an explicit token) is set
- descriptive User-Agent, pinned ``X-GitHub-Api-Version``
- honours primary and secondary rate limits: sleeps until ``x-ratelimit-reset``
  or ``retry-after``, exponential backoff on 5xx / network errors
- pagination via the ``Link`` header
- injectable transport and sleep function so unit tests never touch the network
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from ciburn import __version__

API_VERSION = "2022-11-28"
DEFAULT_BASE_URL = "https://api.github.com"
USER_AGENT = f"ciburn/{__version__} (+https://github.com/addiiiooo/ciburn)"
MAX_RATE_LIMIT_SLEEP = 3600.0


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class NotFoundError(GitHubError):
    pass


class AuthError(GitHubError):
    pass


class RateLimitExhaustedError(GitHubError):
    pass


@dataclass
class RateState:
    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None
    used: int | None = None
    requests_made: int = 0
    seconds_slept: float = 0.0

    def update(self, headers: httpx.Headers) -> None:
        def _int(name: str) -> int | None:
            v = headers.get(name)
            return int(v) if v is not None and v.isdigit() else None

        self.limit = _int("x-ratelimit-limit") or self.limit
        self.remaining = _int("x-ratelimit-remaining")
        self.reset_epoch = _int("x-ratelimit-reset") or self.reset_epoch
        self.used = _int("x-ratelimit-used")


def token_from_env() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(name)
        if v:
            return v.strip()
    return None


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        timeout: float = 30.0,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.token = token if token is not None else token_from_env()
        self.base_url = base_url.rstrip("/")
        self.sleep = sleep
        self.max_retries = max_retries
        self.rate = RateState()
        self.log = log or (lambda _m: None)
        self.clock = clock
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(
            headers=headers, timeout=timeout, transport=transport, follow_redirects=True
        )

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level -------------------------------------------------------------

    def request_raw(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        accept: str | None = None,
    ) -> httpx.Response:
        if not url.startswith("http"):
            url = f"{self.base_url}/{url.lstrip('/')}"
        headers = {"Accept": accept} if accept else None
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.request(method, url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt > self.max_retries:
                    raise GitHubError(
                        f"network error after {attempt - 1} retries: {exc}", url=url
                    ) from exc
                delay = min(2.0 ** (attempt - 1), 60.0)
                self.log(f"network error ({exc}); retrying in {delay:.0f}s")
                self._sleep(delay)
                continue
            self.rate.requests_made += 1
            self.rate.update(resp.headers)
            if resp.status_code < 400:
                return resp
            if resp.status_code == 404:
                raise NotFoundError(_message(resp), 404, url)
            if resp.status_code == 401:
                raise AuthError(_message(resp), 401, url)
            if resp.status_code in (403, 429):
                wait = self._rate_limit_wait(resp, attempt)
                if wait is None:
                    raise GitHubError(_message(resp), resp.status_code, url)
                if attempt > self.max_retries:
                    raise RateLimitExhaustedError(
                        f"rate limited {attempt - 1} times in a row: {_message(resp)}",
                        resp.status_code,
                        url,
                    )
                self.log(f"rate limited ({resp.status_code}); sleeping {wait:.0f}s")
                self._sleep(wait)
                continue
            if resp.status_code >= 500 or resp.status_code == 409:
                if attempt > self.max_retries:
                    raise GitHubError(_message(resp), resp.status_code, url)
                delay = min(2.0 ** (attempt - 1), 60.0)
                self.log(f"server error {resp.status_code}; retrying in {delay:.0f}s")
                self._sleep(delay)
                continue
            raise GitHubError(_message(resp), resp.status_code, url)

    def _rate_limit_wait(self, resp: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait for a 403/429, or None if it is not a rate limit."""
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after) + 1.0, MAX_RATE_LIMIT_SLEEP)
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset and reset.isdigit():
            return max(1.0, min(float(int(reset) - self.clock()) + 2.0, MAX_RATE_LIMIT_SLEEP))
        body = _message(resp).lower()
        if "rate limit" in body or "abuse" in body or resp.status_code == 429:
            # secondary limit: GitHub asks for at least one minute
            return min(60.0 * attempt, 600.0)
        return None

    def _sleep(self, seconds: float) -> None:
        self.rate.seconds_slept += seconds
        self.sleep(seconds)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request_raw("GET", path, params).json()

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        per_page: int = 100,
        max_pages: int | None = None,
        on_page: Callable[[Any], None] | None = None,
    ) -> Iterator[Any]:
        """Yield items across pages. ``key`` is the JSON key holding the list.

        ``on_page`` receives each page's parsed JSON (e.g. to read ``total_count``).
        """
        params = dict(params or {})
        params.setdefault("per_page", per_page)
        url: str | None = path
        page = 0
        while url:
            page += 1
            resp = self.request_raw("GET", url, params if page == 1 else None)
            data = resp.json()
            if on_page is not None:
                on_page(data)
            items = data[key] if key else data
            yield from items
            if max_pages is not None and page >= max_pages:
                return
            url = resp.links.get("next", {}).get("url") if resp.links else None

    # -- endpoints used by ciburn ---------------------------------------------------

    def get_rate_limit(self) -> Any:
        return self.get_json("/rate_limit")

    def get_repo(self, owner: str, repo: str) -> Any:
        return self.get_json(f"/repos/{owner}/{repo}")

    def list_workflows(self, owner: str, repo: str) -> list[Any]:
        return list(self.paginate(f"/repos/{owner}/{repo}/actions/workflows", key="workflows"))

    def list_runs(
        self,
        owner: str,
        repo: str,
        *,
        created_since: str | None = None,
        max_pages: int | None = None,
        extra: dict[str, Any] | None = None,
        on_page: Callable[[Any], None] | None = None,
    ) -> Iterator[Any]:
        params: dict[str, Any] = dict(extra or {})
        if created_since:
            params["created"] = f">={created_since}"
        return self.paginate(
            f"/repos/{owner}/{repo}/actions/runs",
            params,
            key="workflow_runs",
            max_pages=max_pages,
            on_page=on_page,
        )

    def list_jobs(
        self, owner: str, repo: str, run_id: int, *, all_attempts: bool = True
    ) -> list[Any]:
        params = {"filter": "all" if all_attempts else "latest"}
        return list(
            self.paginate(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", params, key="jobs")
        )

    def get_run_timing(self, owner: str, repo: str, run_id: int) -> Any:
        return self.get_json(f"/repos/{owner}/{repo}/actions/runs/{run_id}/timing")

    def get_commit(self, owner: str, repo: str, sha: str) -> Any:
        return self.get_json(f"/repos/{owner}/{repo}/commits/{sha}")

    def get_tree_paths(self, owner: str, repo: str, ref: str) -> tuple[list[str], bool]:
        """All blob paths in the tree at ``ref`` and whether the listing was truncated."""
        data = self.get_json(f"/repos/{owner}/{repo}/git/trees/{ref}", {"recursive": "1"})
        paths = [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]
        return paths, bool(data.get("truncated", False))

    def list_workflow_dir(self, owner: str, repo: str, ref: str) -> list[Any]:
        try:
            data = self.get_json(f"/repos/{owner}/{repo}/contents/.github/workflows", {"ref": ref})
        except NotFoundError:
            return []
        return [d for d in data if d.get("type") == "file"] if isinstance(data, list) else []

    def get_file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
        data = self.get_json(f"/repos/{owner}/{repo}/contents/{path}", {"ref": ref})
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        if isinstance(data, dict) and data.get("download_url"):
            return self.request_raw("GET", str(data["download_url"])).text
        raise GitHubError(f"unexpected contents response for {path}")

    def download_text(self, url: str) -> str:
        """Fetch a raw file (e.g. a ``download_url``); does not consume API quota."""
        return self.request_raw("GET", url, accept="text/plain").text


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and "message" in data:
            return str(data["message"])
    except ValueError:
        pass
    return resp.text[:200] or f"HTTP {resp.status_code}"
