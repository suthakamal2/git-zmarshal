"""Pure deployment-wave policy; persistence, Git and process execution are adapters."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


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


def normalise_train(job: Mapping) -> dict:
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
    out["train_requests"] = [dict(item) for item in out.get("train_requests") or []]
    out["coalesced_count"] = max(1, int(out.get("coalesced_count") or 1))
    return out


def add_train_request(
    job: Mapping,
    *,
    actor: str,
    reason: str,
    idempotency_key: str | None = None,
    revision: str | None = None,
    probe_seconds: int | None = None,
    limit: int = 200,
) -> dict:
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
