#!/usr/bin/env python3
"""Minimal SQLite + subprocess deploy-train runner built on ``git_zmarshal``.

Demonstrates the complete adapter contract in ~230 lines of stdlib Python:
1. ``BEGIN IMMEDIATE`` SQLite transactions for race-free reserve/coalesce.
2. ``plan_deploy_enqueue`` for idempotency deduplication and running/queued coalescing.
3. ``HostAdmissionGuard`` + owner-tagged ``PendingStore`` markers before departure.
4. Departing wave resolves ``HEAD`` at departure and releases DB locks before I/O.
5. ``merge_deploy_result`` + ``verify_wave`` for post-deploy multi-commit ancestry proof.

Run standalone for an end-to-end self-contained demo:
    python examples/minimal_yard.py
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from git_zmarshal.guard import GuardConfig, HostAdmissionGuard, PendingStore
from git_zmarshal.train import (
    DeployTrainPolicy,
    merge_deploy_result,
    normalise_train,
    plan_deploy_enqueue,
)
from git_zmarshal.wave import resolve_revisions, verify_wave

SCHEMA = """
CREATE TABLE IF NOT EXISTS deploy_waves (
    job_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'deploy',
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT,
    source_revision TEXT,
    deployed_revision TEXT,
    coalesced_count INTEGER NOT NULL DEFAULT 1,
    state_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    returncode INTEGER
);
CREATE INDEX IF NOT EXISTS idx_waves_service_status ON deploy_waves(service, status);
"""


class MinimalYard:
    """Reference SQLite + subprocess deploy marshaling yard."""

    def __init__(
        self,
        db_path: str | Path,
        repo: str | Path,
        *,
        pending_dir: str | Path | None = None,
        policy: DeployTrainPolicy | None = None,
        guard: HostAdmissionGuard | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.repo = Path(repo)
        self.pending = PendingStore(pending_dir or (self.db_path.parent / "pending"))
        self.policy = policy or DeployTrainPolicy(window_seconds=5.0, request_limit=200)
        self.guard = guard or HostAdmissionGuard(GuardConfig(proc_root=Path("/proc")))
        with self._tx() as cur:
            cur.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            yield cur
            if conn.in_transaction:
                conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _inflate(row: sqlite3.Row) -> dict:
        data = json.loads(row["state_json"])
        for k in row.keys():
            if k != "state_json":
                data[k] = row[k]
        return normalise_train(data)

    def enqueue(
        self,
        service: str,
        *,
        actor: str,
        reason: str,
        revision: str | None = None,
        idempotency_key: str | None = None,
        force: bool = False,
    ) -> dict:
        """Atomically deduplicate, join a running/queued wave, or open a new queued wave."""
        resolved = resolve_revisions(self.repo, [revision])[0] if revision else None
        with self._tx() as cur:
            rows = cur.execute(
                "SELECT * FROM deploy_waves WHERE service = ? ORDER BY created_at DESC LIMIT 50",
                (service,),
            ).fetchall()
            recent_jobs = [self._inflate(r) for r in rows]
            plan = plan_deploy_enqueue(
                recent_jobs,
                policy=self.policy,
                service=service,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                revision=resolved,
                force=force,
            )
            if plan.action == "deduplicated":
                return {**plan.job, "step": plan.train_step}
            if plan.action in {"join_running", "join_queued"}:
                self._save_state(cur, plan.job)
                return {**plan.job, "step": plan.train_step}

            now = time.time()
            job_id = f"wave-{uuid.uuid4().hex[:8]}"
            initial = dict(plan.job, job_id=job_id, created_at=now)
            cur.execute(
                """INSERT INTO deploy_waves (
                    job_id, service, action, status, actor, reason,
                    idempotency_key, source_revision, coalesced_count, state_json, created_at
                ) VALUES (?, ?, 'deploy', 'queued', ?, ?, ?, ?, 1, ?, ?)""",
                (
                    job_id,
                    service,
                    actor,
                    reason,
                    idempotency_key,
                    resolved,
                    json.dumps(initial),
                    now,
                ),
            )
            return {**initial, "step": plan.train_step}

    @staticmethod
    def _save_state(cur: sqlite3.Cursor, job: dict) -> None:
        cur.execute(
            """UPDATE deploy_waves
               SET reason = ?, source_revision = ?, coalesced_count = ?, state_json = ?
               WHERE job_id = ?""",
            (
                job["reason"],
                job.get("source_revision"),
                job["coalesced_count"],
                json.dumps(job),
                job["job_id"],
            ),
        )

    def step(self, service: str, deploy_cmd: list[str] | str) -> dict | None:
        """Admit and execute the next queued wave for ``service`` without holding DB locks during I/O."""
        cmd = shlex.split(deploy_cmd) if isinstance(deploy_cmd, str) else list(deploy_cmd)
        with self._tx() as cur:
            if cur.execute(
                "SELECT 1 FROM deploy_waves WHERE service = ? AND status = 'running'", (service,)
            ).fetchone():
                return {"status": "busy", "service": service}
            row = cur.execute(
                "SELECT * FROM deploy_waves WHERE service = ? AND status = 'queued' ORDER BY created_at ASC LIMIT 1",
                (service,),
            ).fetchone()
            if row is None:
                return None
            job = self._inflate(row)

            verdict = self.guard.check()
            if not verdict.ok:
                self.pending.record(
                    service,
                    {"action": "deploy"},
                    actor=job["actor"],
                    holds=verdict.holds,
                    owner_id=job["job_id"],
                )
                return {"status": "held", "job_id": job["job_id"], "holds": verdict.holds}

            depart_sha = resolve_revisions(self.repo, ["HEAD"])[0]
            now = time.time()
            job = merge_deploy_result(job, {"status": "running", "source_revision": depart_sha, "started_at": now})
            cur.execute(
                "UPDATE deploy_waves SET status = 'running', source_revision = ?, started_at = ?, state_json = ? WHERE job_id = ?",
                (depart_sha, now, json.dumps(job), job["job_id"]),
            )

        # DB lock is released here before subprocess / network / systemd I/O.
        env = {
            **os.environ,
            "GIT_ZMARSHAL_SERVICE": service,
            "GIT_ZMARSHAL_REVISION": depart_sha,
            "GIT_ZMARSHAL_JOB_ID": job["job_id"],
        }
        try:
            proc = subprocess.run(cmd, cwd=self.repo, env=env, capture_output=True, text=True, check=False)
            status = "succeeded" if proc.returncode == 0 else "failed"
            rc = proc.returncode
        finally:
            self.pending.clear(service, owner_id=job["job_id"])

        finished = time.time()
        with self._tx() as cur:
            latest = cur.execute(
                "SELECT * FROM deploy_waves WHERE job_id = ?", (job["job_id"],)
            ).fetchone()
            final_job = merge_deploy_result(
                self._inflate(latest),
                {
                    "status": status,
                    "deployed_revision": depart_sha if status == "succeeded" else None,
                    "finished_at": finished,
                    "returncode": rc,
                },
            )
            cur.execute(
                """UPDATE deploy_waves
                   SET status = ?, deployed_revision = ?, finished_at = ?, returncode = ?, state_json = ?
                   WHERE job_id = ?""",
                (
                    status,
                    final_job["deployed_revision"],
                    finished,
                    rc,
                    json.dumps(final_job),
                    job["job_id"],
                ),
            )
            return final_job

    def verify(self, job_id: str) -> dict:
        """Prove every commit coalesced into ``job_id`` is an ancestor of its deployed revision."""
        with self._tx() as cur:
            row = cur.execute("SELECT * FROM deploy_waves WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown wave: {job_id}")
            job = self._inflate(row)
        expected = job.get("expected_revisions") or [
            r["revision"] for r in job["train_requests"] if r.get("revision")
        ]
        deployed = job.get("deployed_revision") or ""
        proof = verify_wave(self.repo, expected, deployed)
        return {
            "job_id": job_id,
            "ok": proof.ok,
            "deployed_revision": proof.live_commit,
            "coalesced_count": job["coalesced_count"],
            "results": [{"revision": r.revision, "status": r.status} for r in proof.results],
        }


def run_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="zmarshal-sqlite-yard-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        for args in (
            ["init", "-q", "--initial-branch=main"],
            ["config", "user.name", "Yard Demo"],
            ["config", "user.email", "yard@example.invalid"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True)

        yard = MinimalYard(
            root / "yard.db",
            repo,
            pending_dir=root / "pending",
            guard=HostAdmissionGuard(GuardConfig(proc_root=root / "proc")),
        )
        for idx in range(1, 4):
            subprocess.run(
                ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", f"commit {idx}"],
                check=True,
            )
            sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            res = yard.enqueue(
                "api",
                actor=f"agent-{idx}",
                reason=f"ship commit {idx}",
                revision=sha,
                idempotency_key=f"k-{idx}",
            )
            print(f"[enqueue {idx}] step={res['step']} job_id={res['job_id']} coalesced={res['coalesced_count']}")

        ran = yard.step("api", [sys.executable, "-c", "import os; print(os.environ['GIT_ZMARSHAL_REVISION'])"])
        assert ran is not None and ran["status"] == "succeeded"
        proof = yard.verify(ran["job_id"])
        print(f"[departed]  job_id={ran['job_id']} status={ran['status']} deployed={ran['deployed_revision'][:12]}")
        print(f"[verified]  ok={proof['ok']} commits={[r['status'] for r in proof['results']]}")
        return 0 if proof["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true", help="Run self-contained SQLite + subprocess demo")
    parser.parse_args()
    raise SystemExit(run_demo())
