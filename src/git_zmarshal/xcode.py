"""Xcode / iOS / iPadOS / watchOS build-train coalescing and multi-device wave planning."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

DEFAULT_BRANCH_REFS = frozenset(
    {"", "main", "origin/main", "HEAD", "refs/heads/main", "refs/remotes/origin/main"}
)
DEFAULT_UNIVERSAL_LANE = "universal-app"
UNIVERSAL_APPLE_TARGETS = frozenset({"ios", "ipad", "watch"})
DEFAULT_IPHONE_TARGETS = frozenset({"ios", "watch", "iphone"})
DEFAULT_IPAD_TARGETS = frozenset({"ipad"})
STALE_CLAIM_TIMEOUT_SECONDS: float = 45 * 60.0


class XcodeRequestEntry(TypedDict, total=False):
    target: str
    job_type: str
    git_ref: str | None
    reason: str
    requested_by: str
    launch: bool
    requested_at: str
    expected_revision: str
    idempotency_key: str


class XcodeTrainRecord(TypedDict, total=False):
    lane: list[str]
    coalesced_count: int
    first_enqueued_at: str
    last_enqueued_at: str
    requested_targets: list[str]
    device_kinds: list[str]
    expected_revisions: list[str]
    coalesced_idempotency_keys: list[str]
    train_requests: list[XcodeRequestEntry]
    isolated: bool
    resolved_commit: str
    installed_devices: list[str]
    installed_udids: list[str]


class XcodeJobRecord(TypedDict, total=False):
    id: str
    status: str
    target: str
    job_type: str
    git_ref: str | None
    reason: str
    requested_by: str
    launch: bool
    expected_revision: str | None
    idempotency_key: str | None
    coalesce: bool
    claimed_at: str | None
    created_at: str | None
    result: dict[str, Any]


@dataclass(frozen=True)
class XcodeEnqueuePlan:
    """Decision returned by `plan_xcode_enqueue` for an incoming Xcode build request."""

    action: str  # "insert" | "join_queued" | "join_claimed" | "deduplicated"
    target_job: Mapping[str, Any] | None
    train: dict[str, Any]
    train_step: str  # "started" | "queued_next" | "joined_queued" | "joined_running" | "deduplicated"
    coalesced: bool
    launch: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = _clean_str(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_stale_claimed(
    job: Mapping[str, Any],
    *,
    now_dt: datetime | None = None,
    stale_claim_timeout_seconds: float = STALE_CLAIM_TIMEOUT_SECONDS,
) -> bool:
    if _clean_str(job.get("status")).lower() != "claimed":
        return False
    if stale_claim_timeout_seconds <= 0:
        return False
    claimed_dt = _parse_iso_utc(job.get("claimed_at"))
    if claimed_dt is None:
        return False
    ref_now = now_dt or datetime.now(timezone.utc)
    return (ref_now - claimed_dt).total_seconds() >= stale_claim_timeout_seconds


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_sha(value: Any) -> str | None:
    text = _clean_str(value)
    return text.lower() if _SHA_RE.match(text) else None


def _sha_matches(candidate: str, incoming: str) -> bool:
    cand = _clean_sha(candidate)
    inc = _clean_sha(incoming)
    if not cand or not inc:
        return False
    if len(cand) == 40 and len(inc) == 40:
        return cand == inc
    return cand == inc or cand.startswith(inc) or inc.startswith(cand)


def canonical_xcode_lane(
    target: str = "ios",
    job_type: str = "install",
    *,
    universal_lane: str = DEFAULT_UNIVERSAL_LANE,
    universal_targets: Iterable[str] = UNIVERSAL_APPLE_TARGETS,
) -> tuple[str, str]:
    """Map ``(target, job_type)`` to its canonical Xcode build train lane.

    - A universal iOS/iPadOS app (``TARGETED_DEVICE_FAMILY="1,2"``) that also
      embeds its paired watchOS companion shares one physical-device build lane:
      * ``(ios|ipad|watch, install)`` -> ``(universal_lane, "install")``
      * ``(ios|ipad, compile-check|simulator-test|ota)`` -> ``(universal_lane, job_type)``
    - Standalone watch simulator gates (``("watch", "compile-check"|"simulator-test")``)
      and distinct application schemes keep their own independent lanes.
    """
    norm_target = _clean_str(target) or "ios"
    norm_type = _clean_str(job_type) or "install"
    universal_set = frozenset(universal_targets)
    if norm_type == "install" and norm_target in universal_set:
        return (universal_lane, "install")
    if (
        norm_type in {"compile-check", "simulator-test", "ota"}
        and norm_target in universal_set - {"watch"}
    ):
        return (universal_lane, norm_type)
    return (norm_target, norm_type)


def xcode_build_lane(
    target: str = "ios",
    job_type: str = "install",
    *,
    universal_lane: str = DEFAULT_UNIVERSAL_LANE,
    universal_targets: Iterable[str] = UNIVERSAL_APPLE_TARGETS,
) -> str:
    """Return the compile lane name for ``target`` and ``job_type``."""
    return canonical_xcode_lane(
        target,
        job_type,
        universal_lane=universal_lane,
        universal_targets=universal_targets,
    )[0]


def device_kind_for_target(
    target: str,
    job_type: str = "install",
    *,
    iphone_targets: Iterable[str] = DEFAULT_IPHONE_TARGETS,
    ipad_targets: Iterable[str] = DEFAULT_IPAD_TARGETS,
) -> str | None:
    """Return the physical device kind (``'iphone'`` or ``'ipad'``) required for an install job."""
    norm_target = _clean_str(target) or "ios"
    norm_type = _clean_str(job_type) or "install"
    if norm_type != "install":
        return None
    if norm_target in frozenset(ipad_targets):
        return "ipad"
    if norm_target in frozenset(iphone_targets):
        return "iphone"
    return None


def normalise_git_ref(git_ref: str | None) -> str:
    """Normalise default-branch aliases (None, '', 'main', 'origin/main') to 'main'."""
    cleaned = _clean_str(git_ref)
    if cleaned in DEFAULT_BRANCH_REFS:
        return "main"
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if cleaned in DEFAULT_BRANCH_REFS:
        return "main"
    if _SHA_RE.match(cleaned):
        return cleaned.lower()
    return cleaned


def refs_can_coalesce_queued(existing_ref: str | None, incoming_ref: str | None) -> bool:
    """Return True if two queued requests track the same branch/ref."""
    return normalise_git_ref(existing_ref) == normalise_git_ref(incoming_ref)


def _request_entry(source: Mapping[str, Any], timestamp: str) -> dict[str, Any]:
    target = _clean_str(source.get("target")) or "ios"
    job_type = _clean_str(source.get("job_type")) or "install"
    requested_by = _clean_str(source.get("requested_by") or source.get("actor"))
    entry: dict[str, Any] = {
        "target": target,
        "job_type": job_type,
        "git_ref": _clean_str(source.get("git_ref")) or None,
        "reason": _clean_str(source.get("reason")),
        "requested_by": requested_by,
        "launch": bool(source.get("launch")),
        "requested_at": _clean_str(source.get("requested_at") or source.get("created_at")) or timestamp,
    }
    expected = _clean_sha(source.get("expected_revision"))
    git_ref_str = _clean_str(source.get("git_ref"))
    if expected is None and git_ref_str not in DEFAULT_BRANCH_REFS:
        expected = _clean_sha(git_ref_str)
    if expected:
        entry["expected_revision"] = expected
    key = _clean_str(source.get("idempotency_key"))
    if key:
        entry["idempotency_key"] = key
    return entry


def _dedup_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def normalise_xcode_train(
    job: Mapping[str, Any],
    *,
    now: str | None = None,
    max_requests: int = 50,
    universal_lane: str = DEFAULT_UNIVERSAL_LANE,
    universal_targets: Iterable[str] = UNIVERSAL_APPLE_TARGETS,
    iphone_targets: Iterable[str] = DEFAULT_IPHONE_TARGETS,
    ipad_targets: Iterable[str] = DEFAULT_IPAD_TARGETS,
) -> dict[str, Any]:
    """Return a normalised train metadata dictionary for an Xcode build job."""
    timestamp = now or _utc_now()
    target = _clean_str(job.get("target")) or "ios"
    job_type = _clean_str(job.get("job_type")) or "install"
    lane = list(
        canonical_xcode_lane(
            target,
            job_type,
            universal_lane=universal_lane,
            universal_targets=universal_targets,
        )
    )

    result = job.get("result")
    raw_train = result.get("train") if isinstance(result, Mapping) else None
    raw = dict(raw_train) if isinstance(raw_train, Mapping) else {}

    requests: list[dict[str, Any]] = []
    for r in raw.get("train_requests") or []:
        if isinstance(r, Mapping):
            item = dict(r)
            item.setdefault("requested_at", _clean_str(job.get("created_at")) or timestamp)
            item.setdefault("target", target)
            item.setdefault("job_type", job_type)
            requests.append(item)
    if not requests:
        requests = [_request_entry(job, timestamp)]
    if max_requests > 0 and len(requests) > max_requests:
        requests = requests[-max_requests:]

    targets = _dedup_preserve(
        [str(t) for t in (raw.get("requested_targets") or []) if isinstance(t, str)]
        + [target]
        + [str(r.get("target") or "") for r in requests]
    )

    kinds: list[str] = []
    for existing_kind in raw.get("device_kinds") or []:
        if isinstance(existing_kind, str) and existing_kind in {"iphone", "ipad"}:
            kinds.append(existing_kind)
    for req_target in targets:
        dk = device_kind_for_target(
            req_target,
            job_type,
            iphone_targets=iphone_targets,
            ipad_targets=ipad_targets,
        )
        if dk:
            kinds.append(dk)
    device_kinds = _dedup_preserve(kinds)

    revisions = _dedup_preserve(
        [
            s
            for s in (
                _clean_sha(v)
                for v in [
                    *(raw.get("expected_revisions") or []),
                    job.get("expected_revision"),
                    *(r.get("expected_revision") for r in requests),
                ]
            )
            if s
        ]
    )

    keys = _dedup_preserve(
        [
            k
            for k in (
                _clean_str(v)
                for v in [
                    *(raw.get("coalesced_idempotency_keys") or []),
                    job.get("idempotency_key"),
                    *(r.get("idempotency_key") for r in requests),
                ]
            )
            if k
        ]
    )

    try:
        count = max(int(raw.get("coalesced_count") or 1), len(requests), 1)
    except (TypeError, ValueError):
        count = max(len(requests), 1)

    first_at = (
        _clean_str(raw.get("first_enqueued_at"))
        or _clean_str(requests[0].get("requested_at"))
        or timestamp
    )
    last_at = (
        _clean_str(raw.get("last_enqueued_at"))
        or _clean_str(requests[-1].get("requested_at"))
        or timestamp
    )
    train: dict[str, Any] = {
        "lane": lane,
        "coalesced_count": count,
        "first_enqueued_at": first_at,
        "last_enqueued_at": last_at,
        "requested_targets": targets,
        "device_kinds": device_kinds,
        "expected_revisions": revisions,
        "coalesced_idempotency_keys": keys,
        "train_requests": requests,
    }
    if bool(raw.get("isolated")) or job.get("coalesce") is False:
        train["isolated"] = True
    resolved_commit = _clean_sha(
        (result.get("commit") if isinstance(result, Mapping) else None)
        or raw.get("resolved_commit")
    )
    if resolved_commit:
        train["resolved_commit"] = resolved_commit
    if isinstance(raw.get("installed_devices"), list):
        train["installed_devices"] = [str(d) for d in raw["installed_devices"] if d]
    if isinstance(raw.get("installed_udids"), list):
        train["installed_udids"] = [str(u) for u in raw["installed_udids"] if u]
    return train


def add_xcode_train_request(
    job: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    now: str | None = None,
    max_requests: int = 50,
    universal_lane: str = DEFAULT_UNIVERSAL_LANE,
    universal_targets: Iterable[str] = UNIVERSAL_APPLE_TARGETS,
    iphone_targets: Iterable[str] = DEFAULT_IPHONE_TARGETS,
    ipad_targets: Iterable[str] = DEFAULT_IPAD_TARGETS,
) -> dict[str, Any]:
    """Coalesce an incoming Xcode build request into `job`'s train metadata."""
    timestamp = now or _utc_now()
    train = normalise_xcode_train(
        job,
        now=timestamp,
        max_requests=max_requests,
        universal_lane=universal_lane,
        universal_targets=universal_targets,
        iphone_targets=iphone_targets,
        ipad_targets=ipad_targets,
    )
    entry = _request_entry(incoming, timestamp)
    job_type = _clean_str(job.get("job_type")) or "install"

    key = entry.get("idempotency_key")
    duplicate = bool(key and key in train["coalesced_idempotency_keys"])
    if not duplicate:
        train["train_requests"].append(entry)
        if max_requests > 0 and len(train["train_requests"]) > max_requests:
            train["train_requests"] = train["train_requests"][-max_requests:]
        train["coalesced_count"] = max(train["coalesced_count"] + 1, len(train["train_requests"]))
        train["last_enqueued_at"] = entry["requested_at"]

    if key and key not in train["coalesced_idempotency_keys"]:
        train["coalesced_idempotency_keys"].append(key)

    req_target = entry["target"]
    if req_target and req_target not in train["requested_targets"]:
        train["requested_targets"].append(req_target)

    dk = device_kind_for_target(
        req_target,
        job_type,
        iphone_targets=iphone_targets,
        ipad_targets=ipad_targets,
    )
    if dk and dk not in train["device_kinds"]:
        train["device_kinds"].append(dk)

    expected = entry.get("expected_revision")
    if expected and expected not in train["expected_revisions"]:
        train["expected_revisions"].append(expected)

    return train


def can_join_claimed_xcode_job(
    claimed_job: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    now: str | None = None,
    stale_claim_timeout_seconds: float = STALE_CLAIM_TIMEOUT_SECONDS,
    universal_lane: str = DEFAULT_UNIVERSAL_LANE,
    universal_targets: Iterable[str] = UNIVERSAL_APPLE_TARGETS,
    iphone_targets: Iterable[str] = DEFAULT_IPHONE_TARGETS,
    ipad_targets: Iterable[str] = DEFAULT_IPAD_TARGETS,
) -> bool:
    """Return True only when an in-flight ('claimed') Xcode job provably satisfies `incoming`."""
    if claimed_job.get("status") != "claimed":
        return False
    now_dt = _parse_iso_utc(now) if now else datetime.now(timezone.utc)
    if _is_stale_claimed(
        claimed_job,
        now_dt=now_dt,
        stale_claim_timeout_seconds=stale_claim_timeout_seconds,
    ):
        return False
    claimed_lane = canonical_xcode_lane(
        str(claimed_job.get("target") or "ios"),
        str(claimed_job.get("job_type") or "install"),
        universal_lane=universal_lane,
        universal_targets=universal_targets,
    )
    incoming_lane = canonical_xcode_lane(
        str(incoming.get("target") or "ios"),
        str(incoming.get("job_type") or "install"),
        universal_lane=universal_lane,
        universal_targets=universal_targets,
    )
    if claimed_lane != incoming_lane:
        return False
    if not refs_can_coalesce_queued(claimed_job.get("git_ref"), incoming.get("git_ref")):
        return False
    if bool(incoming.get("launch")) and not bool(claimed_job.get("launch")):
        return False

    result = claimed_job.get("result")
    result_map = result if isinstance(result, Mapping) else {}
    train = normalise_xcode_train(
        claimed_job,
        now=now,
        universal_lane=universal_lane,
        universal_targets=universal_targets,
        iphone_targets=iphone_targets,
        ipad_targets=ipad_targets,
    )
    if train.get("isolated"):
        return False

    observed_commit = _clean_sha(result_map.get("commit")) or _clean_sha(train.get("resolved_commit"))
    if observed_commit:
        known_shas = [observed_commit]
    else:
        candidate_shas: list[str | None] = [
            *(_clean_sha(rev) for rev in (train.get("expected_revisions") or [])),
        ]
        claimed_ref = _clean_str(claimed_job.get("git_ref"))
        if claimed_ref not in DEFAULT_BRANCH_REFS and _clean_sha(claimed_ref):
            candidate_shas.append(_clean_sha(claimed_ref))
        known_shas = [s for s in candidate_shas if s]

    incoming_sha = _clean_sha(incoming.get("expected_revision"))
    incoming_ref = _clean_str(incoming.get("git_ref"))
    if incoming_sha is None and incoming_ref not in DEFAULT_BRANCH_REFS:
        incoming_sha = _clean_sha(incoming_ref)
    if not known_shas or not incoming_sha:
        return False
    if not any(_sha_matches(sha, incoming_sha) for sha in known_shas):
        return False

    needed_kind = device_kind_for_target(
        str(incoming.get("target") or "ios"),
        str(incoming.get("job_type") or "install"),
        iphone_targets=iphone_targets,
        ipad_targets=ipad_targets,
    )
    if needed_kind is not None and needed_kind not in train["device_kinds"]:
        return False
    return True


def plan_xcode_enqueue(
    active_jobs: Iterable[Mapping[str, Any]],
    incoming: Mapping[str, Any] | None = None,
    *,
    target: str | None = None,
    job_type: str | None = None,
    git_ref: str | None = None,
    reason: str | None = None,
    requested_by: str | None = None,
    actor: str | None = None,
    launch: bool | None = None,
    expected_revision: str | None = None,
    idempotency_key: str | None = None,
    coalesce: bool | None = None,
    now: str | None = None,
    max_requests: int = 50,
    stale_claim_timeout_seconds: float = STALE_CLAIM_TIMEOUT_SECONDS,
    universal_lane: str = DEFAULT_UNIVERSAL_LANE,
    universal_targets: Iterable[str] = UNIVERSAL_APPLE_TARGETS,
    iphone_targets: Iterable[str] = DEFAULT_IPHONE_TARGETS,
    ipad_targets: Iterable[str] = DEFAULT_IPAD_TARGETS,
) -> XcodeEnqueuePlan:
    """Decide whether an incoming request deduplicates, joins a claimed/queued wave, or inserts a new wave.

    Accepts either a positional ``incoming`` mapping or keyword arguments (``target=..., job_type=...``).
    """
    req: dict[str, Any] = dict(incoming) if isinstance(incoming, Mapping) else {}
    for field_name, val in (
        ("target", target),
        ("job_type", job_type),
        ("git_ref", git_ref),
        ("reason", reason),
        ("requested_by", requested_by or actor),
        ("launch", launch),
        ("expected_revision", expected_revision),
        ("idempotency_key", idempotency_key),
        ("coalesce", coalesce),
    ):
        if val is not None:
            req[field_name] = val

    timestamp = now or _utc_now()
    now_dt = _parse_iso_utc(timestamp) or datetime.now(timezone.utc)
    all_jobs = list(active_jobs)
    key = _clean_str(req.get("idempotency_key"))
    if key:
        for job in all_jobs:
            train = normalise_xcode_train(
                job,
                now=timestamp,
                max_requests=max_requests,
                universal_lane=universal_lane,
                universal_targets=universal_targets,
                iphone_targets=iphone_targets,
                ipad_targets=ipad_targets,
            )
            if key in train["coalesced_idempotency_keys"]:
                return XcodeEnqueuePlan(
                    action="deduplicated",
                    target_job=job,
                    train=train,
                    train_step="deduplicated",
                    coalesced=True,
                    launch=bool(job.get("launch")),
                )

    jobs = [
        job
        for job in all_jobs
        if not _is_stale_claimed(
            job,
            now_dt=now_dt,
            stale_claim_timeout_seconds=stale_claim_timeout_seconds,
        )
    ]

    if req.get("coalesce") is False:
        return XcodeEnqueuePlan(
            action="insert",
            target_job=None,
            train=normalise_xcode_train(
                req,
                now=timestamp,
                max_requests=max_requests,
                universal_lane=universal_lane,
                universal_targets=universal_targets,
                iphone_targets=iphone_targets,
                ipad_targets=ipad_targets,
            ),
            train_step="started",
            coalesced=False,
            launch=bool(req.get("launch")),
        )

    incoming_lane = canonical_xcode_lane(
        str(req.get("target") or "ios"),
        str(req.get("job_type") or "install"),
        universal_lane=universal_lane,
        universal_targets=universal_targets,
    )

    # 1. If a claimed job on this lane already checked out the exact expected SHA, join it.
    claimed_on_lane: list[Mapping[str, Any]] = []
    for job in jobs:
        if job.get("status") != "claimed":
            continue
        lane = canonical_xcode_lane(
            str(job.get("target") or "ios"),
            str(job.get("job_type") or "install"),
            universal_lane=universal_lane,
            universal_targets=universal_targets,
        )
        if lane == incoming_lane:
            claimed_on_lane.append(job)
            if can_join_claimed_xcode_job(
                job,
                req,
                now=timestamp,
                stale_claim_timeout_seconds=stale_claim_timeout_seconds,
                universal_lane=universal_lane,
                universal_targets=universal_targets,
                iphone_targets=iphone_targets,
                ipad_targets=ipad_targets,
            ):
                updated = add_xcode_train_request(
                    job,
                    req,
                    now=timestamp,
                    max_requests=max_requests,
                    universal_lane=universal_lane,
                    universal_targets=universal_targets,
                    iphone_targets=iphone_targets,
                    ipad_targets=ipad_targets,
                )
                return XcodeEnqueuePlan(
                    action="join_claimed",
                    target_job=job,
                    train=updated,
                    train_step="joined_running",
                    coalesced=True,
                    launch=bool(job.get("launch")),
                )

    # 2. If a queued wave exists on this lane for the same branch/ref track, coalesce into it.
    for job in jobs:
        if job.get("status") != "queued":
            continue
        job_train = normalise_xcode_train(
            job,
            now=timestamp,
            max_requests=max_requests,
            universal_lane=universal_lane,
            universal_targets=universal_targets,
            iphone_targets=iphone_targets,
            ipad_targets=ipad_targets,
        )
        if job_train.get("isolated"):
            continue
        lane = canonical_xcode_lane(
            str(job.get("target") or "ios"),
            str(job.get("job_type") or "install"),
            universal_lane=universal_lane,
            universal_targets=universal_targets,
        )
        if lane == incoming_lane and refs_can_coalesce_queued(
            job.get("git_ref"), req.get("git_ref")
        ):
            updated = add_xcode_train_request(
                job,
                req,
                now=timestamp,
                max_requests=max_requests,
                universal_lane=universal_lane,
                universal_targets=universal_targets,
                iphone_targets=iphone_targets,
                ipad_targets=ipad_targets,
            )
            return XcodeEnqueuePlan(
                action="join_queued",
                target_job=job,
                train=updated,
                train_step="joined_queued",
                coalesced=True,
                launch=bool(job.get("launch") or req.get("launch")),
            )

    # 3. Otherwise open a new queued wave.
    step = "queued_next" if claimed_on_lane else "started"
    return XcodeEnqueuePlan(
        action="insert",
        target_job=None,
        train=normalise_xcode_train(
            req,
            now=timestamp,
            max_requests=max_requests,
            universal_lane=universal_lane,
            universal_targets=universal_targets,
            iphone_targets=iphone_targets,
            ipad_targets=ipad_targets,
        ),
        train_step=step,
        coalesced=False,
        launch=bool(req.get("launch")),
    )


def merge_xcode_result(
    existing_result: Mapping[str, Any] | None,
    reported_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge a worker's reported result dict while preserving server-owned `train` metadata."""
    merged: dict[str, Any] = {}
    if isinstance(existing_result, Mapping):
        merged.update(existing_result)
    if isinstance(reported_result, Mapping):
        for k, v in reported_result.items():
            if k != "train":
                merged[k] = v
    existing_train = (
        dict(existing_result["train"])
        if isinstance(existing_result, Mapping) and isinstance(existing_result.get("train"), Mapping)
        else None
    )
    reported_train = (
        dict(reported_result["train"])
        if isinstance(reported_result, Mapping) and isinstance(reported_result.get("train"), Mapping)
        else None
    )
    train = existing_train if existing_train is not None else reported_train
    if train is not None:
        commit = _clean_sha(merged.get("commit"))
        if commit:
            train["resolved_commit"] = commit
        installed_devices = merged.get("installed_devices")
        if isinstance(installed_devices, list):
            train["installed_devices"] = [str(d) for d in installed_devices if d]
        installed_udids = merged.get("installed_udids")
        if isinstance(installed_udids, list):
            train["installed_udids"] = [str(u) for u in installed_udids if u]
        merged["train"] = train
    return merged
