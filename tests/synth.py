"""Synthetic run/job history builder for join and history-rule tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ciburn.history.store import Store
from ciburn.history.view import HistoryView
from ciburn.pricing import Pricing
from ciburn.workflow import Workflow

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Synth:
    def __init__(self, full_name: str = "acme/widgets", private: bool = False) -> None:
        self.store = Store()
        self.repo_id = self.store.upsert_repo(
            {"id": 42, "full_name": full_name, "private": private, "default_branch": "main"}
        )
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self._rid = 1000
        self._jid = 50000

    def run(
        self,
        path: str,
        event: str = "push",
        conclusion: str = "success",
        at: datetime | None = None,
        head_sha: str = "abc1234",
        branch: str = "main",
        attempt: int = 1,
        status: str = "completed",
        wall_seconds: float = 300,
    ) -> int:
        self._rid += 1
        at = at or T0 + timedelta(hours=len(self.runs))
        self.runs.append(
            {
                "id": self._rid,
                "workflow_id": 1,
                "name": path,
                "path": path,
                "event": event,
                "status": status,
                "conclusion": conclusion,
                "run_attempt": attempt,
                "run_number": self._rid,
                "head_sha": head_sha,
                "head_branch": branch,
                "created_at": ts(at),
                "run_started_at": ts(at),
                "updated_at": ts(at + timedelta(seconds=wall_seconds)),
                "display_title": "t",
                "actor": {"login": "x"},
            }
        )
        self.jobs[self._rid] = []
        return self._rid

    def job(
        self,
        run_id: int,
        name: str,
        seconds: float = 120,
        conclusion: str = "success",
        labels: list[str] | None = None,
        runner_name: str = "GitHub Actions 7",
        attempt: int = 1,
        queue_seconds: float = 5,
        steps: list[tuple[str, float]] | None = None,
        start_offset: float = 0,
    ) -> int:
        self._jid += 1
        run = next(r for r in self.runs if r["id"] == run_id)
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) + timedelta(
            seconds=start_offset
        )
        started = created + timedelta(seconds=queue_seconds)
        completed = started + timedelta(seconds=seconds)
        step_rows = []
        cursor = started
        for i, (sname, ssec) in enumerate(steps or [], 1):
            step_rows.append(
                {
                    "name": sname,
                    "status": "completed",
                    "conclusion": "success",
                    "number": i,
                    "started_at": ts(cursor),
                    "completed_at": ts(cursor + timedelta(seconds=ssec)),
                }
            )
            cursor += timedelta(seconds=ssec)
        self.jobs[run_id].append(
            {
                "id": self._jid,
                "run_id": run_id,
                "run_attempt": attempt,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "created_at": ts(created),
                "started_at": ts(started),
                "completed_at": ts(completed),
                "labels": labels or ["ubuntu-latest"],
                "runner_id": 1,
                "runner_name": runner_name,
                "runner_group_name": "GitHub Actions",
                "workflow_name": run["name"],
                "head_sha": run["head_sha"],
                "steps": step_rows,
            }
        )
        return self._jid

    def commit(self, sha: str, files: list[str]) -> None:
        self.store.upsert_commit(self.repo_id, sha, files, 1, 1)

    def view(
        self, pricing: Pricing, workflows: list[Workflow], model: str = "2026", days: int = 90
    ) -> HistoryView:
        self.store.upsert_runs(self.repo_id, self.runs)
        for rid, jobs in self.jobs.items():
            self.store.upsert_jobs(self.repo_id, rid, jobs)
            self.store.mark_jobs_fetched(rid, "completed")
        return HistoryView.from_store(
            self.store,
            "acme/widgets",
            pricing,
            pricing.model(model),
            workflows=workflows,
            window_days=days,
            now=NOW,
        )
