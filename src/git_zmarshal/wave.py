"""Service-wave planning and bounded, fail-closed Git ancestry verification.

These helpers never fetch, mutate a checkout or deploy anything. Callers supply
the revision reported by their deployment system and decide how to handle an
unknown proof (for example, explicitly fetch missing history and retry).
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


class GitVerificationError(RuntimeError):
    """Git cannot provide a trustworthy answer within the requested deadline."""


@dataclass(frozen=True)
class CommitVerification:
    revision: str
    commit: str | None
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"equal", "ancestor"}


@dataclass(frozen=True)
class WaveVerification:
    live_revision: str
    live_commit: str | None
    results: tuple[CommitVerification, ...]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(result.ok for result in self.results)

    @property
    def unknown(self) -> bool:
        return any(result.status == "unknown" for result in self.results)


def plan_wave(
    services: Iterable[str], lock_groups: Mapping[str, str] | None = None
) -> list[str]:
    """Deduplicate services and interleave groups while preserving group order.

    This is a submission order, not permission to run jobs concurrently. The
    caller must still respect its admission, locking and completion contracts.
    """
    groups = lock_groups or {}
    queues: dict[str, deque[str]] = {}
    for service in dict.fromkeys(services):
        queues.setdefault(groups.get(service, service), deque()).append(service)
    ordered: list[str] = []
    while any(queues.values()):
        ordered.extend(queue.popleft() for queue in queues.values() if queue)
    return ordered


class _Git:
    def __init__(self, repo: str | Path, timeout: float):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.repo = Path(repo)
        self.deadline = time.monotonic() + timeout

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GitVerificationError("Git verification deadline expired")
        try:
            # Git hooks and embedded callers can export a different repository.
            # The explicit repo argument must own the proof's object graph.
            environment = dict(os.environ)
            for key in (
                "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_NAMESPACE", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE",
            ):
                environment.pop(key, None)
            return subprocess.run(
                ["git", "--no-replace-objects", *args],
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=environment,
                timeout=remaining,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise GitVerificationError(f"Git unavailable: {exc}") from exc

    def resolve(self, revision: str) -> str:
        if not isinstance(revision, str) or not revision.strip() or "\0" in revision:
            raise GitVerificationError("Expected a non-empty Git revision")
        result = self.run("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
        commit = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            raise GitVerificationError(f"Cannot resolve commit {revision!r}")
        return commit


def _revisions(revisions: Iterable[str]) -> tuple[str, ...]:
    if isinstance(revisions, (str, bytes)):
        raise ValueError("expected_revisions must be an iterable of revision strings")
    values = tuple(dict.fromkeys(revisions))
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("At least one non-empty expected revision is required")
    return values


def resolve_revisions(
    repo: str | Path, revisions: Iterable[str], *, timeout: float = 10.0
) -> tuple[str, ...]:
    """Freeze revision names to full commit IDs before submitting a wave.

    Raises ``GitVerificationError`` for missing/ambiguous objects, invalid
    revisions or Git errors; ``ValueError`` for an empty request/bad timeout.
    The timeout covers the whole operation, not each individual Git call.
    """
    values = _revisions(revisions)
    git = _Git(repo, timeout)
    return tuple(dict.fromkeys(git.resolve(value) for value in values))


def _verify_commit(git: _Git, revision: str, live_commit: str) -> CommitVerification:
    commit = None
    try:
        commit = git.resolve(revision)
        if commit == live_commit:
            return CommitVerification(revision, commit, "equal")
        result = git.run("merge-base", "--is-ancestor", commit, live_commit)
        if result.returncode == 0:
            return CommitVerification(revision, commit, "ancestor")
        if result.returncode != 1:
            raise GitVerificationError("Git ancestry check failed")
        shallow = git.run("rev-parse", "--is-shallow-repository")
        if shallow.returncode or shallow.stdout.strip() != "false":
            raise GitVerificationError("Ancestry is incomplete or unavailable in this checkout")
        return CommitVerification(revision, commit, "not-ancestor")
    except GitVerificationError as exc:
        return CommitVerification(revision, commit, "unknown", str(exc))


def verify_wave(
    repo: str | Path,
    expected_revisions: Iterable[str],
    live_revision: str,
    *,
    timeout: float = 10.0,
) -> WaveVerification:
    """Prove every requested commit is contained in one fixed deployed revision.

    ``equal``/``ancestor`` are proofs; ``not-ancestor`` is a negative proof in a
    complete local history; ``unknown`` covers missing/ambiguous objects,
    shallow negative results, invalid live revisions and Git errors/timeouts.
    Textual prefix equality is never sufficient. Git replacement objects are
    disabled, and the live revision is resolved once before checking the wave.
    The timeout bounds all Git subprocesses collectively. No network I/O occurs.

    This proves commit ancestry only, not running-image identity, health or
    application behaviour. A later revert can undo an ancestor's changes.
    """
    revisions = _revisions(expected_revisions)
    git = _Git(repo, timeout)
    try:
        live_commit = git.resolve(live_revision)
    except GitVerificationError as exc:
        return WaveVerification(
            live_revision, None,
            tuple(CommitVerification(revision, None, "unknown", str(exc)) for revision in revisions),
        )
    return WaveVerification(
        live_revision, live_commit,
        tuple(_verify_commit(git, revision, live_commit) for revision in revisions),
    )
