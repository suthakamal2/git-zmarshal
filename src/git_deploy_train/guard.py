"""Linux (/proc) and macOS/POSIX host admission and advisory pending markers, using only the stdlib.

Markers are cooperative scheduling hints, not authorisation or durable jobs.
All writers must use the same store: a stable flock inode serialises processes,
and replacement makes readers see complete records without taking a lock.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_PROGRAMS = {
    "pg_dump": "a logical database dump holds relation locks",
    "pg_dumpall": "a logical database dump holds relation locks",
    "pg_basebackup": "a database base backup uses disk bandwidth",
    "restic": "a backup or prune uses disk bandwidth",
}
XCODE_PROGRAMS = {
    "xcodebuild": "an Xcode build uses CPU and disk bandwidth",
    "xcodegen": "project generation mutates workspace state",
}
DEFAULT_PYTHON_PROGRAMS = {"pytest": "a test run may compete for host resources"}
_GIT_MAINTENANCE = frozenset({"gc", "prune", "repack", "pack-objects", "fsck"})
_GIT_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_WRITE_LOCK = threading.RLock()
_UNSPECIFIED = object()
_DEFAULT_PROC = Path("/proc")


def classify_command(tokens, *, programs=None, python_programs=None):
    """Classify the executed program; never search arbitrary command arguments."""
    if not tokens:
        return None
    programs = DEFAULT_PROGRAMS if programs is None else programs
    python_programs = DEFAULT_PYTHON_PROGRAMS if python_programs is None else python_programs
    program = PurePosixPath(tokens[0]).name
    if program in programs:
        return f"{program} running ({programs[program]})"
    if program.removesuffix(".py") in python_programs:
        entry = program.removesuffix(".py")
        return f"{entry} running ({python_programs[entry]})"
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", program):
        entry = _python_entrypoint(tokens)
        if entry in python_programs:
            return f"{entry} running ({python_programs[entry]})"
    if program == "git":
        command = _git_subcommand(tokens)
        if command in _GIT_MAINTENANCE:
            return f"git {command} running (repacks the object store; CPU+IO heavy)"
    return None


def _python_entrypoint(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-c") or token == "-":
            return None
        if token == "-m":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token.startswith("-m"):
            return token[2:]
        if token in {"-W", "-X"}:
            index += 2
        elif token == "--":
            return (
                PurePosixPath(tokens[index + 1]).name.removesuffix(".py")
                if index + 1 < len(tokens)
                else None
            )
        elif token.startswith("-"):
            index += 1
        else:
            return PurePosixPath(token).name.removesuffix(".py")
    return None


def _git_subcommand(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return token
        index += 2 if token in _GIT_VALUE_FLAGS else 1
    return None


def _scan_ps_text(text: str, *, classifier=classify_command, ignore_pid: int | None = None):
    producers = []
    self_pid = os.getpid() if ignore_pid is None else ignore_pid
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = parts[0]
        if int(pid) == self_pid:
            continue
        try:
            tokens = shlex.split(parts[1])
        except ValueError:
            tokens = parts[1].split()
        reason = classifier(tokens)
        if reason:
            producers.append((pid, reason))
    return producers


def scan_producers(
    proc_root=_DEFAULT_PROC,
    *,
    classifier=classify_command,
    ps_output: str | None = None,
    allow_ps_fallback: bool | None = None,
):
    """Return ``[(pid, reason)]`` from ``/proc`` on Linux or ``ps`` fallback on macOS/POSIX."""
    if ps_output is not None:
        return _scan_ps_text(ps_output, classifier=classifier)
    root = Path(proc_root)
    if root.is_dir():
        try:
            pids = sorted((p.name for p in root.iterdir() if p.name.isdigit()), key=int)
        except OSError:
            return []
        producers = []
        for pid in pids:
            try:
                raw = (root / pid / "cmdline").read_bytes()
            except OSError:
                continue  # the process can exit between directory and cmdline reads
            reason = classifier([s for s in raw.decode("utf-8", "replace").split("\0") if s])
            if reason:
                producers.append((pid, reason))
        return producers

    use_ps = allow_ps_fallback if allow_ps_fallback is not None else (root == _DEFAULT_PROC)
    if not use_ps:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,args="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return _scan_ps_text(proc.stdout, classifier=classifier)


def pytest_cgroup_exempt(text, *, admitted_components=()):
    """Recognise cgroup path components, not substring lookalikes.

    Docker, containerd, Kubernetes and Actions job scopes are exempt. This
    assumes the operator has bounded those cgroups; membership cannot prove a
    resource limit exists. Explicit additional component names are supported.
    """
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        for part in fields[2].split("/"):
            if part in admitted_components or part in {
                "docker",
                "containerd",
                "kubepods",
                "actions_job",
            }:
                return True
            if re.fullmatch(r"(?:docker|cri-containerd|containerd)-[a-zA-Z0-9]+\.scope", part):
                return True
            if re.fullmatch(r"kubepods(?:[-_][a-zA-Z0-9_.-]+)?\.slice", part):
                return True
            if re.fullmatch(r"actions_job(?:[-_][a-zA-Z0-9_.-]+)?(?:\.scope|\.slice)?", part):
                return True
    return False


@dataclass(frozen=True)
class GuardConfig:
    proc_root: Path = _DEFAULT_PROC
    admitted_cgroup_components: tuple[str, ...] = ()
    admitted_programs: frozenset[str] = frozenset()
    max_load_per_cpu: float | None = None
    max_swap_used_ratio: float | None = None
    pressure_limit: float | None = None
    cpu_count: int | None = None
    include_xcode_programs: bool = False
    ps_output: str | None = None
    darwin_swapusage: str | None = None

    def __post_init__(self):
        if self.cpu_count is not None and (
            not isinstance(self.cpu_count, int)
            or isinstance(self.cpu_count, bool)
            or self.cpu_count <= 0
        ):
            raise ValueError("cpu_count must be a positive integer")
        for name, maximum in (
            ("max_load_per_cpu", math.inf),
            ("max_swap_used_ratio", 1),
            ("pressure_limit", 100),
        ):
            value = getattr(self, name)
            if value is not None and not _valid_threshold(value, maximum):
                raise ValueError(f"{name} must be finite and between 0 and {maximum}")


def _valid_threshold(value, maximum):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= maximum
    except OverflowError:
        return False


@dataclass
class HostVerdict:
    ok: bool
    holds: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


class HostAdmissionGuard:
    """Configurable contender, load, swap and pressure checks; unknown is open."""

    def __init__(self, config=None):
        self.config = config or GuardConfig()

    def check(self):
        config = self.config
        holds = []
        detail = {"producers": []}
        programs = (
            {**DEFAULT_PROGRAMS, **XCODE_PROGRAMS}
            if config.include_xcode_programs
            else DEFAULT_PROGRAMS
        )
        for pid, why in scan_producers(
            config.proc_root,
            classifier=lambda tokens: classify_command(tokens, programs=programs),
            ps_output=config.ps_output,
        ):
            kind = why.partition(" running")[0]
            if kind in config.admitted_programs:
                continue
            if kind == "pytest" and self._pytest_exempt(pid):
                continue
            detail["producers"].append({"pid": pid, "why": why})
            holds.append(why)
        _resource_checks(config, holds, detail)
        return HostVerdict(not holds, holds, detail)

    def _pytest_exempt(self, pid):
        try:
            text = (Path(self.config.proc_root) / pid / "cgroup").read_text()
        except OSError:
            return False
        return pytest_cgroup_exempt(
            text, admitted_components=self.config.admitted_cgroup_components
        )


def _resource_checks(config, holds, detail):
    root = Path(config.proc_root)
    load = None
    try:
        load = float((root / "loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        if root == _DEFAULT_PROC and not root.exists() and hasattr(os, "getloadavg"):
            try:
                load = float(os.getloadavg()[0])
            except OSError:
                load = None
    if load is not None and math.isfinite(load) and load >= 0:
        count = config.cpu_count or _host_cpu_count(root)
        detail["load_per_cpu"] = load / count
        if config.max_load_per_cpu is not None and load / count > config.max_load_per_cpu:
            holds.append(
                f"host load per CPU {load / count:.2f} exceeds {config.max_load_per_cpu}"
            )
    _swap_check(
        root,
        config.max_swap_used_ratio,
        holds,
        detail,
        darwin_swapusage=config.darwin_swapusage,
    )
    if config.pressure_limit is not None:
        for resource in ("io", "memory"):
            try:
                text = (root / "pressure" / resource).read_text()
            except OSError:
                continue
            match = re.search(r"^full\s+avg10=([\d.]+)", text, re.MULTILINE)
            try:
                if match and float(match[1]) > config.pressure_limit:
                    holds.append(f"host {resource} full pressure exceeds {config.pressure_limit}%")
            except ValueError:
                pass


def _host_cpu_count(root):
    try:
        count = sum(
            bool(re.match(r"^cpu\d+\s", line))
            for line in (root / "stat").read_text().splitlines()
        )
    except OSError:
        count = 0
    return count or os.cpu_count() or 1


def _parse_darwin_swapusage(text: str) -> tuple[float, float] | None:
    """Parse ``sysctl vm.swapusage`` output into ``(total_mb, free_mb)``."""
    unit_scale = {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
    total_m = re.search(r"total\s*=\s*([\d.]+)\s*([KMGT])", text, re.IGNORECASE)
    free_m = re.search(r"free\s*=\s*([\d.]+)\s*([KMGT])", text, re.IGNORECASE)
    if not total_m or not free_m:
        return None
    try:
        total = float(total_m.group(1)) * unit_scale[total_m.group(2).upper()]
        free = float(free_m.group(1)) * unit_scale[free_m.group(2).upper()]
    except (ValueError, KeyError):
        return None
    return (total, free)


def _swap_check(root, limit, holds, detail, *, darwin_swapusage: str | None = None):
    total: float | None = None
    free: float | None = None
    try:
        values = {
            line.split(":", 1)[0]: int(line.split()[1])
            for line in (root / "meminfo").read_text().splitlines()
            if line.startswith(("SwapTotal:", "SwapFree:"))
        }
        total, free = float(values["SwapTotal"]), float(values["SwapFree"])
    except (OSError, ValueError, IndexError, KeyError):
        raw_darwin = darwin_swapusage
        if (
            raw_darwin is None
            and root == _DEFAULT_PROC
            and not root.exists()
            and sys.platform == "darwin"
            and limit is not None
        ):
            try:
                proc = subprocess.run(
                    ["sysctl", "-n", "vm.swapusage"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if proc.returncode == 0:
                    raw_darwin = proc.stdout
            except (OSError, subprocess.SubprocessError):
                raw_darwin = None
        if raw_darwin:
            parsed = _parse_darwin_swapusage(raw_darwin)
            if parsed is not None:
                total, free = parsed

    if total is None or free is None or total <= 0 or not 0 <= free <= total:
        return
    ratio = (total - free) / total
    detail["swap_used_ratio"] = ratio
    if limit is not None and ratio > limit:
        holds.append(f"host swap used {ratio:.1%} exceeds {limit:.1%}")


@contextmanager
def serialised_file(path):
    """Lock a stable sidecar inode; never unlink the lock file after use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        with path.with_name(path.name + ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_json_write(path, record, *, mode=0o644):
    """Replace atomically using a unique temp name, cleaning up on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), mode)
            json.dump(record, output, allow_nan=False)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _number(value):
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and math.isfinite(value)
    )


def pending_live(record, now, max_ttl_s=3600):
    if not isinstance(record, dict) or not isinstance(record.get("service"), str):
        return False
    if not isinstance(record.get("payload"), dict) or not isinstance(
        record.get("caller_meta"), dict
    ):
        return False
    if not isinstance(record.get("holds"), list) or not _number(record.get("first_denied_at")):
        return False
    expiry = record.get("expires_at")
    return _number(expiry) and now < expiry <= now + max_ttl_s


def _service_name(service):
    if not isinstance(service, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service):
        raise ValueError(f"unsafe service name for a pending marker: {service!r}")
    return service


class PendingStore:
    """Per-service advisory markers with process-safe refresh and ownership."""

    def __init__(self, directory, *, ttl_s=1800, max_ttl_s=3600):
        self.directory = Path(directory)
        self.ttl_s = max(1, min(int(ttl_s), int(max_ttl_s)))
        self.max_ttl_s = int(max_ttl_s)

    def _path(self, service):
        return self.directory / f"{_service_name(service)}.json"

    def read(self, service, *, now=None):
        now = time.time() if now is None else now
        record = _read_json(self._path(service))
        if pending_live(record, now, self.max_ttl_s) and record["service"] == service:
            return record
        return None

    def record(
        self,
        service,
        payload=None,
        *,
        actor="",
        holds=None,
        caller_meta=None,
        owner_id=None,
        now=None,
    ):
        now = time.time() if now is None else now
        path = self._path(service)
        with serialised_file(path):
            previous = self.read(service, now=now) or {}
            if previous.get("owner_id") != owner_id:
                previous = {}
            denials = previous.get("denials", 0)
            record = {
                "service": service,
                "marker_id": uuid.uuid4().hex,
                "payload": dict(payload or {}),
                "actor": actor,
                "caller_meta": dict(caller_meta or {}),
                "holds": list(holds or [])[:20],
                "first_denied_at": previous.get("first_denied_at", now),
                "refreshed_at": now,
                "expires_at": now + self.ttl_s,
                "denials": (denials if isinstance(denials, int) else 0) + 1,
            }
            if owner_id is not None:
                record["owner_id"] = str(owner_id)
                record["job_id"] = str(owner_id)
            atomic_json_write(path, record)
        return record

    def clear(self, service, *, owner_id=None, marker_id=_UNSPECIFIED):
        """Clear conditionally by owner and/or a captured marker generation.

        Omitted marker_id means unconditional legacy clearing. Explicit None
        matches only a legacy marker without an ID, never a newer write.
        """
        path = self._path(service)
        with serialised_file(path):
            record = _read_json(path)
            if owner_id is not None and (
                not isinstance(record, dict) or record.get("owner_id") != str(owner_id)
            ):
                return False
            if marker_id is not _UNSPECIFIED and (
                not isinstance(record, dict) or record.get("marker_id") != marker_id
            ):
                return False
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

    def markers(self, *, now=None):
        now = time.time() if now is None else now
        live, dead = [], []
        for path in sorted(self.directory.glob("*.json")):
            record = _read_json(path)
            if pending_live(record, now, self.max_ttl_s) and record["service"] == path.stem:
                live.append(record)
            else:
                dead.append(path)
        return live, dead

    def prune(self, *, now=None, candidates=None):
        removed = []
        candidates = self.markers(now=now)[1] if candidates is None else candidates
        for path in candidates:
            path = Path(path)
            if path.parent != self.directory or path.suffix != ".json":
                continue
            with serialised_file(path):
                record = _read_json(path)
                current = time.time() if now is None else now
                if pending_live(record, current, self.max_ttl_s) and record["service"] == path.stem:
                    continue
                try:
                    path.unlink()
                    removed.append(path)
                except FileNotFoundError:
                    pass
        return removed


class PendingFlag:
    """Single ``deploy-pending.json`` flag with compare-and-clear ownership."""

    def __init__(self, path, *, ttl_s=1800):
        self.path = Path(path)
        self.ttl_s = max(1, min(int(ttl_s), 3600))

    def record(self, job_id, *, service="deploy", now=None):
        now = time.time() if now is None else now
        record = {"job_id": str(job_id), "service": service, "expires_at": now + self.ttl_s}
        with serialised_file(self.path):
            atomic_json_write(self.path, record)
        return record

    def read(self, *, now=None):
        now = time.time() if now is None else now
        record = _read_json(self.path)
        if isinstance(record, dict) and isinstance(record.get("job_id"), str):
            expiry = record.get("expires_at")
            if _number(expiry) and now < expiry <= now + 3600:
                return record
        return None

    def clear(self, job_id):
        with serialised_file(self.path):
            record = _read_json(self.path)
            if not isinstance(record, dict) or record.get("job_id") != str(job_id):
                return False
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False
