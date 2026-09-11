"""SQLite cache of everything fetched from GitHub. One file, many repositories."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY, full_name TEXT UNIQUE NOT NULL, private INTEGER, default_branch TEXT,
  language TEXT, stargazers INTEGER, pushed_at TEXT, fetched_at TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS workflows (
  id INTEGER NOT NULL, repo_id INTEGER NOT NULL, name TEXT, path TEXT, state TEXT,
  created_at TEXT, updated_at TEXT, PRIMARY KEY (repo_id, id));
CREATE TABLE IF NOT EXISTS workflow_files (
  repo_id INTEGER NOT NULL, path TEXT NOT NULL, ref TEXT, sha TEXT, content TEXT, fetched_at TEXT,
  PRIMARY KEY (repo_id, path));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, workflow_id INTEGER, name TEXT, path TEXT,
  event TEXT, status TEXT, conclusion TEXT, run_attempt INTEGER, run_number INTEGER,
  head_sha TEXT, head_branch TEXT, created_at TEXT, run_started_at TEXT, updated_at TEXT,
  display_title TEXT, actor TEXT, jobs_fetched_at TEXT, jobs_fetched_status TEXT);
CREATE INDEX IF NOT EXISTS runs_repo_created ON runs (repo_id, created_at);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, repo_id INTEGER NOT NULL, run_attempt INTEGER,
  name TEXT, status TEXT, conclusion TEXT, created_at TEXT, started_at TEXT, completed_at TEXT,
  labels TEXT, runner_id INTEGER, runner_name TEXT, runner_group_name TEXT, workflow_name TEXT,
  head_sha TEXT);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs (run_id);
CREATE INDEX IF NOT EXISTS jobs_repo ON jobs (repo_id);
CREATE TABLE IF NOT EXISTS steps (
  job_id INTEGER NOT NULL, number INTEGER NOT NULL, name TEXT, status TEXT, conclusion TEXT,
  started_at TEXT, completed_at TEXT, PRIMARY KEY (job_id, number));
CREATE TABLE IF NOT EXISTS commits (
  repo_id INTEGER NOT NULL, sha TEXT NOT NULL, files TEXT, additions INTEGER, deletions INTEGER,
  fetched_at TEXT, PRIMARY KEY (repo_id, sha));
CREATE TABLE IF NOT EXISTS timings (
  run_id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, billable TEXT, run_duration_ms INTEGER,
  fetched_at TEXT);
CREATE TABLE IF NOT EXISTS fetch_state (
  repo_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (repo_id, key));
"""


def default_cache_path() -> Path:
    env = os.environ.get("CIBURN_CACHE")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "ciburn" / "history.sqlite"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = Path(path) if path != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path) if self.path is None else str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL") if self.path else None
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- repos --------------------------------------------------------------------

    def upsert_repo(self, data: dict[str, Any]) -> int:
        rid = int(data["id"])
        self.conn.execute(
            """INSERT INTO repos (id, full_name, private, default_branch, language, stargazers, pushed_at,
                                  fetched_at, raw)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name, private=excluded.private,
                 default_branch=excluded.default_branch, language=excluded.language,
                 stargazers=excluded.stargazers, pushed_at=excluded.pushed_at,
                 fetched_at=excluded.fetched_at, raw=excluded.raw""",
            (
                rid,
                data["full_name"],
                1 if data.get("private") else 0,
                data.get("default_branch"),
                data.get("language"),
                data.get("stargazers_count"),
                data.get("pushed_at"),
                now_iso(),
                json.dumps(
                    {
                        k: data.get(k)
                        for k in (
                            "id",
                            "full_name",
                            "private",
                            "default_branch",
                            "language",
                            "stargazers_count",
                            "pushed_at",
                            "archived",
                            "fork",
                            "size",
                            "created_at",
                        )
                    }
                ),
            ),
        )
        self.conn.commit()
        return rid

    def get_repo(self, full_name: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM repos WHERE lower(full_name)=lower(?)", (full_name,)
        ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def list_repos(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM repos ORDER BY full_name"))

    # -- workflows --------------------------------------------------------------------

    def upsert_workflows(self, repo_id: int, items: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT INTO workflows (id, repo_id, name, path, state, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(repo_id, id) DO UPDATE SET name=excluded.name, path=excluded.path,
                 state=excluded.state, updated_at=excluded.updated_at""",
            [
                (
                    int(w["id"]),
                    repo_id,
                    w.get("name"),
                    w.get("path"),
                    w.get("state"),
                    w.get("created_at"),
                    w.get("updated_at"),
                )
                for w in items
            ],
        )
        self.conn.commit()

    def workflows(self, repo_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM workflows WHERE repo_id=? ORDER BY path", (repo_id,))
        )

    def upsert_workflow_file(
        self, repo_id: int, path: str, ref: str, sha: str, content: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO workflow_files (repo_id, path, ref, sha, content, fetched_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(repo_id, path) DO UPDATE SET ref=excluded.ref, sha=excluded.sha,
                 content=excluded.content, fetched_at=excluded.fetched_at""",
            (repo_id, path, ref, sha, content, now_iso()),
        )
        self.conn.commit()

    def workflow_file_sha(self, repo_id: int, path: str) -> str | None:
        row = self.conn.execute(
            "SELECT sha FROM workflow_files WHERE repo_id=? AND path=?", (repo_id, path)
        ).fetchone()
        return str(row["sha"]) if row else None

    def workflow_files(self, repo_id: int) -> list[tuple[str, str]]:
        return [
            (str(r["path"]), str(r["content"]))
            for r in self.conn.execute(
                "SELECT path, content FROM workflow_files WHERE repo_id=? ORDER BY path", (repo_id,)
            )
        ]

    def delete_workflow_files_not_in(self, repo_id: int, keep: Iterable[str]) -> None:
        keep_set = set(keep)
        rows = self.conn.execute(
            "SELECT path FROM workflow_files WHERE repo_id=?", (repo_id,)
        ).fetchall()
        for r in rows:
            if r["path"] not in keep_set:
                self.conn.execute(
                    "DELETE FROM workflow_files WHERE repo_id=? AND path=?", (repo_id, r["path"])
                )
        self.conn.commit()

    # -- runs / jobs / steps ------------------------------------------------------------

    def upsert_runs(self, repo_id: int, items: Iterable[dict[str, Any]]) -> int:
        rows = []
        for r in items:
            actor = r.get("actor") or {}
            rows.append(
                (
                    int(r["id"]),
                    repo_id,
                    r.get("workflow_id"),
                    r.get("name"),
                    r.get("path"),
                    r.get("event"),
                    r.get("status"),
                    r.get("conclusion"),
                    r.get("run_attempt"),
                    r.get("run_number"),
                    r.get("head_sha"),
                    r.get("head_branch"),
                    r.get("created_at"),
                    r.get("run_started_at"),
                    r.get("updated_at"),
                    r.get("display_title"),
                    actor.get("login") if isinstance(actor, dict) else None,
                )
            )
        self.conn.executemany(
            """INSERT INTO runs (id, repo_id, workflow_id, name, path, event, status, conclusion, run_attempt,
                                 run_number, head_sha, head_branch, created_at, run_started_at, updated_at,
                                 display_title, actor)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, conclusion=excluded.conclusion,
                 run_attempt=excluded.run_attempt, updated_at=excluded.updated_at,
                 run_started_at=excluded.run_started_at, name=excluded.name, path=excluded.path""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def runs_needing_jobs(self, repo_id: int, since_iso: str) -> list[sqlite3.Row]:
        """Runs in the window whose jobs were never fetched, or were fetched before the run completed."""
        return list(
            self.conn.execute(
                """SELECT * FROM runs WHERE repo_id=? AND created_at>=?
                   AND (jobs_fetched_at IS NULL OR jobs_fetched_status IS NULL
                        OR jobs_fetched_status NOT IN ('completed', 'gone'))
                   ORDER BY created_at DESC""",
                (repo_id, since_iso),
            )
        )

    def runs(self, repo_id: int, since_iso: str | None = None) -> list[sqlite3.Row]:
        if since_iso:
            return list(
                self.conn.execute(
                    "SELECT * FROM runs WHERE repo_id=? AND created_at>=? ORDER BY created_at",
                    (repo_id, since_iso),
                )
            )
        return list(
            self.conn.execute("SELECT * FROM runs WHERE repo_id=? ORDER BY created_at", (repo_id,))
        )

    def mark_jobs_fetched(self, run_id: int, run_status: str | None) -> None:
        self.conn.execute(
            "UPDATE runs SET jobs_fetched_at=?, jobs_fetched_status=? WHERE id=?",
            (now_iso(), run_status, run_id),
        )
        self.conn.commit()

    def upsert_jobs(self, repo_id: int, run_id: int, items: Iterable[dict[str, Any]]) -> int:
        n = 0
        for j in items:
            n += 1
            self.conn.execute(
                """INSERT INTO jobs (id, run_id, repo_id, run_attempt, name, status, conclusion, created_at,
                                     started_at, completed_at, labels, runner_id, runner_name, runner_group_name,
                                     workflow_name, head_sha)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, conclusion=excluded.conclusion,
                     started_at=excluded.started_at, completed_at=excluded.completed_at, labels=excluded.labels,
                     runner_id=excluded.runner_id, runner_name=excluded.runner_name,
                     runner_group_name=excluded.runner_group_name""",
                (
                    int(j["id"]),
                    run_id,
                    repo_id,
                    j.get("run_attempt"),
                    j.get("name"),
                    j.get("status"),
                    j.get("conclusion"),
                    j.get("created_at"),
                    j.get("started_at"),
                    j.get("completed_at"),
                    json.dumps(j.get("labels") or []),
                    j.get("runner_id"),
                    j.get("runner_name"),
                    j.get("runner_group_name"),
                    j.get("workflow_name"),
                    j.get("head_sha"),
                ),
            )
            steps = j.get("steps") or []
            self.conn.execute("DELETE FROM steps WHERE job_id=?", (int(j["id"]),))
            self.conn.executemany(
                "INSERT OR REPLACE INTO steps (job_id, number, name, status, conclusion, started_at, completed_at)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        int(j["id"]),
                        int(s.get("number", i + 1)),
                        s.get("name"),
                        s.get("status"),
                        s.get("conclusion"),
                        s.get("started_at"),
                        s.get("completed_at"),
                    )
                    for i, s in enumerate(steps)
                ],
            )
        self.conn.commit()
        return n

    def jobs_for_runs(self, run_ids: Iterable[int]) -> Iterator[sqlite3.Row]:
        ids = list(run_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = f"SELECT * FROM jobs WHERE run_id IN ({','.join('?' * len(chunk))}) ORDER BY run_id, id"
            yield from self.conn.execute(q, chunk)

    def steps_for_jobs(self, job_ids: Iterable[int]) -> dict[int, list[sqlite3.Row]]:
        ids = list(job_ids)
        out: dict[int, list[sqlite3.Row]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = f"SELECT * FROM steps WHERE job_id IN ({','.join('?' * len(chunk))}) ORDER BY job_id, number"
            for row in self.conn.execute(q, chunk):
                out.setdefault(int(row["job_id"]), []).append(row)
        return out

    # -- commits / timings / state ----------------------------------------------------------

    def upsert_commit(
        self, repo_id: int, sha: str, files: list[str], additions: int, deletions: int
    ) -> None:
        self.conn.execute(
            """INSERT INTO commits (repo_id, sha, files, additions, deletions, fetched_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(repo_id, sha) DO UPDATE SET files=excluded.files, additions=excluded.additions,
                 deletions=excluded.deletions, fetched_at=excluded.fetched_at""",
            (repo_id, sha, json.dumps(files), additions, deletions, now_iso()),
        )
        self.conn.commit()

    def commit_files(self, repo_id: int) -> dict[str, list[str]]:
        return {
            str(r["sha"]): list(json.loads(r["files"] or "[]"))
            for r in self.conn.execute("SELECT sha, files FROM commits WHERE repo_id=?", (repo_id,))
        }

    def has_commit(self, repo_id: int, sha: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM commits WHERE repo_id=? AND sha=?", (repo_id, sha)
            ).fetchone()
            is not None
        )

    def upsert_timing(
        self, repo_id: int, run_id: int, billable: dict[str, Any], run_duration_ms: int | None
    ) -> None:
        self.conn.execute(
            """INSERT INTO timings (run_id, repo_id, billable, run_duration_ms, fetched_at) VALUES (?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET billable=excluded.billable, run_duration_ms=excluded.run_duration_ms,
                 fetched_at=excluded.fetched_at""",
            (run_id, repo_id, json.dumps(billable), run_duration_ms, now_iso()),
        )
        self.conn.commit()

    def timings(self, repo_id: int) -> dict[int, dict[str, Any]]:
        return {
            int(r["run_id"]): json.loads(r["billable"] or "{}")
            for r in self.conn.execute(
                "SELECT run_id, billable FROM timings WHERE repo_id=?", (repo_id,)
            )
        }

    def set_state(self, repo_id: int, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO fetch_state (repo_id, key, value) VALUES (?,?,?) ON CONFLICT(repo_id, key) DO UPDATE SET value=excluded.value",
            (repo_id, key, json.dumps(value)),
        )
        self.conn.commit()

    def get_state(self, repo_id: int, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM fetch_state WHERE repo_id=? AND key=?", (repo_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def counts(self, repo_id: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("workflows", "runs", "jobs", "commits", "workflow_files"):
            out[table] = int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE repo_id=?", (repo_id,)
                ).fetchone()[0]
            )
        return out
