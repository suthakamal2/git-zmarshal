"""Pure deployment-wave policy and queue planner; persistence, Git and process execution are adapters."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

STALE_RUNNING_TIMEOUT_SECONDS: float = 60 * 60.0


class TrainRequestEntry(TypedDict, total=False):
    actor: str
    reason: str
    idempotency_key: str | None
    revision: str | None
    requested_at: str


class DeployWaveRecord(TypedDict, total=False):
    job_id: str
    service: str
    action: str
    status: str
    actor: str
    reason: str
    idempotency_key: str | None
    source_revision: str | None
    deployed_revision: str | None
    expected_revision: str | None
    expected_revisions: list[str]
    coalesced_count: int
    coalesced_actors: list[str]
    coalesced_reasons: list[str]
    coalesced_idempotency_keys: list[str]
    train_requests: list[TrainRequestEntry]
    probe_seconds: int
    isolated: bool


@dataclass(frozen=True)
class DeployEnqueuePlan:
    """Decision returned by ``plan_deploy_enqueue`` for an incoming deploy request."""

    action: str  # "insert" | "join_queued" | "join_running" | "deduplicated"
    target_job: Mapping[str, Any] | None
    job: dict[str, Any]
    train_step: str  # "started" | "queued_next" | "joined_queued" | "joined_running" | "deduplicated"
    coalesced: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _is_stale_running(
    job: Mapping[str, Any],
    *,
    now_dt: datetime,
    stale_running_timeout_seconds: float = STALE_RUNNING_TIMEOUT_SECONDS,
) -> bool:
    if str(job.get("status") or "").strip().lower() not in {"running", "accepted"}:
        return False
    if stale_running_timeout_seconds <= 0:
        return False
    started_dt = _parse_timestamp(job.get("started_at") or job.get("created_at"))
    if started_dt is None:
        return False
    return (now_dt - started_dt).total_seconds() >= stale_running_timeout_seconds


@dataclass(frozen=True)
class DeployTrainPolicy:
    enabled: bool = True
    window_seconds: float = 0.0
    request_limit: int = 200

    def __post_init__(self):
        if not math.isfinite(self.window_seconds) or self.window_seconds < 0:
            raise ValueError("window_seconds must be finite and non-negative")
        if self.request_limit < 1:
            raise ValueError("request_limit must be positive")

    def eligible(
        self,
        *,
        action="deploy",
        force=False,
        fresh=False,
        expected_revision=None,
        pending_retry=False,
    ) -> bool:
        return bool(
            self.enabled
            and action == "deploy"
            and not force
            and not fresh
            and expected_revision is None
            and not pending_retry
        )

    def can_share_running(self, job: Mapping, observed_revision: str | None) -> bool:
        # An accepted reservation has not yet fixed the checkout used by its unit.
        return bool(
            job.get("status") == "running"
            and job.get("action") == "deploy"
            and not job.get("isolated")
            and job.get("expected_revision") is None
            and observed_revision
            and job.get("source_revision") == observed_revision
        )

    def window_remaining(self, created_at: str | None, now: float, override=None) -> int:
        window = self.window_seconds if override is None else float(override)
        if not math.isfinite(window) or window < 0:
            raise ValueError("window_seconds must be finite and non-negative")
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created  # noqa: UP017 -- Python 3.10
        except ValueError:
            return 0
        return max(0, math.ceil(window - (now - created.timestamp())))


def normalise_train(job: Mapping[str, Any]) -> dict[str, Any]:
    """Copy legacy/current state without sharing mutable request lists."""
    out = dict(job)
    for field, fallback in (
        ("coalesced_actors", "actor"),
        ("coalesced_reasons", "reason"),
        ("coalesced_idempotency_keys", "idempotency_key"),
    ):
        values = list(out.get(field) or [])
        if (
            out.get(fallback)
            and (not values or field == "coalesced_idempotency_keys")
            and out[fallback] not in values
        ):
            values.insert(0, out[fallback])
        out[field] = values

    requests = [dict(item) for item in out.get("train_requests") or [] if isinstance(item, Mapping)]
    if not requests and (
        out.get("actor") or out.get("reason") or out.get("idempotency_key") or out.get("source_revision")
    ):
        requests = [
            {
                "actor": out.get("actor") or "",
                "reason": out.get("reason") or "",
                "idempotency_key": out.get("idempotency_key"),
                "revision": out.get("source_revision"),
            }
        ]
    out["train_requests"] = requests

    expected_revs: list[str] = []
    for rev in (
        *(out.get("expected_revisions") or []),
        out.get("source_revision"),
        *(r.get("revision") for r in requests),
    ):
        if isinstance(rev, str) and rev.strip() and rev.strip() not in expected_revs:
            expected_revs.append(rev.strip())
    out["expected_revisions"] = expected_revs
    out["coalesced_count"] = max(1, int(out.get("coalesced_count") or 1))
    if out.get("coalesce") is False or bool(out.get("isolated")):
        out["isolated"] = True
    return out


def add_train_request(
    job: Mapping[str, Any],
    *,
    actor: str,
    reason: str,
    idempotency_key: str | None = None,
    revision: str | None = None,
    probe_seconds: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Coalesce one distinct request. All retry keys survive bounded audit history.

    Revision observations need not be ordered: a queued revision is informational
    and must be resolved again at departure. A running job's revision is immutable.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    out = normalise_train(job)
    keys = out["coalesced_idempotency_keys"]
    if idempotency_key and idempotency_key in keys:
        return out
    if idempotency_key:
        keys.append(idempotency_key)
    for field, value in (("coalesced_actors", actor), ("coalesced_reasons", reason)):
        if value and value not in out[field]:
            out[field].append(value)
    if isinstance(revision, str) and revision.strip() and revision.strip() not in out["expected_revisions"]:
        out["expected_revisions"].append(revision.strip())
    out["coalesced_count"] += 1
    out["train_requests"].append(
        {"actor": actor, "reason": reason, "idempotency_key": idempotency_key, "revision": revision}
    )
    out["train_requests"] = out["train_requests"][-limit:]
    out["reason"] = "; ".join(out["coalesced_reasons"])[:300]
    if out.get("status") == "queued":
        out["source_revision"] = revision
        if probe_seconds is not None:
            current_probe = int(out.get("probe_seconds") or 0)
            out["probe_seconds"] = max(current_probe, int(probe_seconds))
    return out


def plan_deploy_enqueue(
    active_jobs: Iterable[Mapping[str, Any]],
    incoming: Mapping[str, Any] | None = None,
    *,
    policy: DeployTrainPolicy | None = None,
    service: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    revision: str | None = None,
    expected_revision: str | None = None,
    probe_seconds: int | None = None,
    force: bool | None = None,
    fresh: bool | None = None,
    pending_retry: bool | None = None,
    coalesce: bool | None = None,
    now: str | float | None = None,
    stale_running_timeout_seconds: float = STALE_RUNNING_TIMEOUT_SECONDS,
) -> DeployEnqueuePlan:
    """Decide whether an incoming deploy request deduplicates, joins a running/queued wave, or inserts a new wave.

    Mirrors ``plan_xcode_enqueue`` for server/container deployment trains. Accepts either
    a positional ``incoming`` mapping or keyword arguments.
    """
    train_policy = policy or DeployTrainPolicy()
    req: dict[str, Any] = dict(incoming) if isinstance(incoming, Mapping) else {}
    for field_name, val in (
        ("service", service),
        ("action", action),
        ("actor", actor),
        ("reason", reason),
        ("idempotency_key", idempotency_key),
        ("source_revision", revision),
        ("expected_revision", expected_revision),
        ("probe_seconds", probe_seconds),
        ("force", force),
        ("fresh", fresh),
        ("pending_retry", pending_retry),
        ("coalesce", coalesce),
    ):
        if val is not None:
            req[field_name] = val

    req.setdefault("action", "deploy")
    req.setdefault("status", "queued")
    req.setdefault("actor", "")
    req.setdefault("reason", "")
    observed_rev = req.get("source_revision") or req.get("revision")
    if observed_rev is not None:
        req["source_revision"] = observed_rev

    req_service = str(req.get("service") or "").strip()
    key = str(req.get("idempotency_key") or "").strip() or None
    now_dt = _parse_timestamp(now if now is not None else _utc_now()) or datetime.now(timezone.utc)
    all_jobs = list(active_jobs)

    def _same_service(candidate: Mapping[str, Any]) -> bool:
        cand_svc = str(candidate.get("service") or "").strip()
        return not req_service or not cand_svc or cand_svc == req_service

    # 1. Deduplicate by idempotency_key across active (and any supplied terminal) waves
    if key:
        for job in all_jobs:
            if not _same_service(job):
                continue
            norm = normalise_train(job)
            if key in norm["coalesced_idempotency_keys"]:
                return DeployEnqueuePlan(
                    action="deduplicated",
                    target_job=job,
                    job=norm,
                    train_step="deduplicated",
                    coalesced=True,
                )

    live_jobs = [
        job
        for job in all_jobs
        if _same_service(job)
        and not _is_stale_running(
            job,
            now_dt=now_dt,
            stale_running_timeout_seconds=stale_running_timeout_seconds,
        )
    ]

    eligible = (
        req.get("coalesce") is not False
        and not bool(req.get("isolated"))
        and train_policy.eligible(
            action=req.get("action", "deploy"),
            force=bool(req.get("force")),
            fresh=bool(req.get("fresh")),
            expected_revision=req.get("expected_revision"),
            pending_retry=bool(req.get("pending_retry")),
        )
    )
    if not eligible:
        isolated_req = dict(req, isolated=True)
        return DeployEnqueuePlan(
            action="insert",
            target_job=None,
            job=normalise_train(isolated_req),
            train_step="started",
            coalesced=False,
        )

    # 2. Attach to a running wave when revisions match and the wave is not isolated
    active_in_flight = False
    for job in live_jobs:
        status = str(job.get("status") or "").strip().lower()
        if status in {"running", "accepted"}:
            active_in_flight = True
        if status == "running" and train_policy.can_share_running(job, observed_rev):
            updated = add_train_request(
                job,
                actor=str(req.get("actor") or ""),
                reason=str(req.get("reason") or ""),
                idempotency_key=key,
                revision=observed_rev,
                probe_seconds=req.get("probe_seconds"),
                limit=train_policy.request_limit,
            )
            return DeployEnqueuePlan(
                action="join_running",
                target_job=job,
                job=updated,
                train_step="joined_running",
                coalesced=True,
            )

    # 3. Coalesce into an existing queued wave for the same service
    for job in live_jobs:
        if str(job.get("status") or "").strip().lower() != "queued":
            continue
        norm_job = normalise_train(job)
        if norm_job.get("isolated") or norm_job.get("expected_revision") is not None:
            continue
        updated = add_train_request(
            norm_job,
            actor=str(req.get("actor") or ""),
            reason=str(req.get("reason") or ""),
            idempotency_key=key,
            revision=observed_rev,
            probe_seconds=req.get("probe_seconds"),
            limit=train_policy.request_limit,
        )
        return DeployEnqueuePlan(
            action="join_queued",
            target_job=job,
            job=updated,
            train_step="joined_queued",
            coalesced=True,
        )

    # 4. Otherwise start a new queued wave
    step = "queued_next" if active_in_flight else "started"
    return DeployEnqueuePlan(
        action="insert",
        target_job=None,
        job=normalise_train(req),
        train_step=step,
        coalesced=False,
    )


def merge_deploy_result(
    existing_job: Mapping[str, Any] | None,
    reported_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge a worker's terminal/in-flight deploy update while preserving server-owned train metadata."""
    base = normalise_train(existing_job or {})
    if not isinstance(reported_result, Mapping):
        return base
    preserved_keys = {
        "coalesced_count",
        "coalesced_actors",
        "coalesced_reasons",
        "coalesced_idempotency_keys",
        "train_requests",
        "expected_revisions",
        "isolated",
    }
    for k, v in reported_result.items():
        if k not in preserved_keys:
            base[k] = v
    commit = reported_result.get("deployed_revision") or reported_result.get("source_revision")
    if isinstance(commit, str) and commit.strip():
        base.setdefault("deployed_revision", commit.strip())
    return base
