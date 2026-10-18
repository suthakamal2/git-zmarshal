"""Opt-in cooperative pytest yielding: ``-p git_zmarshal.pytest_plugin``.

New runs wait before collection. In-progress runs stop after a test's teardown
when a pending marker appears, with pytest's interrupted exit status (2), so a
partial suite cannot be mistaken for a passing one. The runner should retry it.
No pytest import is needed: core library users do not depend on pytest.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import sys
import time
from pathlib import Path

try:
    from pytest import hookimpl
except ImportError:  # the standalone waiter also works without pytest installed
    def hookimpl(**options):
        def decorate(function):
            function.pytest_impl = options
            return function
        return decorate

if __package__:
    from . import guard
else:  # re-exec the waiter by file path, including from an isolated interpreter
    _spec = importlib.util.spec_from_file_location(
        "_git_zmarshal_guard", Path(__file__).with_name("guard.py")
    )
    guard = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = guard
    _spec.loader.exec_module(guard)

BYPASS_ENV = "GIT_ZMARSHAL_BYPASS"
LEGACY_BYPASS_ENV = "GIT_DEPLOY_TRAIN_BYPASS"
CHECKED_ENV = "GIT_ZMARSHAL_CHECKED"
LEGACY_CHECKED_ENV = "GIT_DEPLOY_TRAIN_CHECKED"
MAX_WAIT_ENV = "GIT_ZMARSHAL_MAX_WAIT"
LEGACY_MAX_WAIT_ENV = "GIT_DEPLOY_TRAIN_MAX_WAIT"
PENDING_DIR_ENV = "GIT_ZMARSHAL_PENDING_DIR"
LEGACY_PENDING_DIR_ENV = "GIT_DEPLOY_TRAIN_PENDING_DIR"
PENDING_FILE_ENV = "GIT_ZMARSHAL_PENDING_FILE"
LEGACY_PENDING_FILE_ENV = "GIT_DEPLOY_TRAIN_PENDING_FILE"
DEFAULT_PENDING_DIR = "/run/git-zmarshal/pending"
DEFAULT_MAX_WAIT_S = 900.0
POLL_S = 5.0
_TRUTHY = {"1", "true", "yes", "on"}


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        val = os.environ.get(name)
        if val is not None:
            return val
    return default


def _log(message):
    print(f"[deploy-pending] {message}", file=sys.stderr, flush=True)


def bounded_wait(value, *, default=DEFAULT_MAX_WAIT_S, maximum=3600):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0.0, min(value, maximum)) if math.isfinite(value) else default


def live_markers():
    try:
        raw_dir = _env_first(PENDING_DIR_ENV, LEGACY_PENDING_DIR_ENV, default=DEFAULT_PENDING_DIR)
        directory = Path(raw_dir or DEFAULT_PENDING_DIR)
        markers = guard.PendingStore(directory).markers()[0]
        markers += guard.PendingStore(directory / "trains").markers()[0]
        flag = _env_first(PENDING_FILE_ENV, LEGACY_PENDING_FILE_ENV)
        record = guard.PendingFlag(flag).read() if flag else None
        if record:
            markers.append(record)
        return markers
    except (OSError, ValueError):
        return []


def describe(markers):
    parts = []
    now = time.time()
    for record in markers:
        try:
            age = max(0, int(now - float(record.get("first_denied_at", now))))
        except (TypeError, ValueError, OverflowError):
            age = 0
        holds = "; ".join(str(hold) for hold in record.get("holds", [])[:3]) or "?"
        parts.append(
            f"{record.get('service', 'deploy')} (pending {age}s, requested by {record.get('actor') or '?'}; held by: {holds})"
        )
    return ", ".join(parts)


def _is_bypassed(bypass_env: str = BYPASS_ENV) -> bool:
    candidates = (bypass_env, LEGACY_BYPASS_ENV) if bypass_env == BYPASS_ENV else (bypass_env,)
    return any(os.environ.get(name, "").strip().lower() in _TRUTHY for name in candidates)


def wait_for_clear(
    *,
    max_wait=None,
    poll=POLL_S,
    sleep=time.sleep,
    clock=time.monotonic,
    read_markers=None,
    bypass_env=BYPASS_ENV,
    max_wait_env=MAX_WAIT_ENV,
    log=_log,
):
    """Wait with an injected reader/clock; invalid timing inputs remain bounded."""
    read_markers = live_markers if read_markers is None else read_markers
    if _is_bypassed(bypass_env):
        if read_markers():
            log(f"{bypass_env} set: not waiting for the pending deploy")
        return "bypassed"
    markers = read_markers()
    if not markers:
        return "clear"
    raw_max_wait = (
        _env_first(max_wait_env, LEGACY_MAX_WAIT_ENV, default=str(DEFAULT_MAX_WAIT_S))
        if max_wait is None
        else max_wait
    )
    cap = bounded_wait(raw_max_wait)
    poll = max(0.001, bounded_wait(poll, default=POLL_S))
    start = last_note = clock()
    log(
        f"deploy pending: {describe(markers)}. Waiting up to {cap:.0f}s before starting this test run "
        f"so the deploy wave can admit it (bypass: {bypass_env}=1)."
    )
    while markers:
        waited = clock() - start
        if waited >= cap:
            log(f"wait cap of {cap:.0f}s reached with a deploy still pending; starting the test run anyway")
            return "capped"
        sleep(min(poll, cap - waited))
        markers = read_markers()
        if markers and clock() - last_note >= 60:
            last_note = clock()
            log(f"still waiting ({clock() - start:.0f}s): {describe(markers)}")
    log(f"deploy no longer pending after {clock() - start:.0f}s; starting the test run")
    return "cleared"


def own_cmdline():
    try:
        return [
            token
            for token in Path("/proc/self/cmdline")
            .read_bytes()
            .decode("utf-8", "surrogateescape")
            .split("\0")
            if token
        ]
    except OSError:
        orig = getattr(sys, "orig_argv", None)
        if orig:
            return [str(t) for t in orig if t]
        return [sys.executable, *sys.argv] if sys.argv else []


def _exempt():
    try:
        return guard.pytest_cgroup_exempt(Path("/proc/self/cgroup").read_text())
    except OSError:
        return False


def launch_environ(checked_env=CHECKED_ENV):
    """Replay launch-time environment, avoiding pytest-cov re-exec state leaks."""
    try:
        raw = Path("/proc/self/environ").read_bytes()
        env = dict(item.partition(b"=")[::2] for item in raw.split(b"\0") if b"=" in item)
    except OSError:
        env = {key: value for key, value in os.environb.items() if not key.startswith(b"COV_CORE_")}
    env[checked_env.encode()] = b"1"
    env[LEGACY_CHECKED_ENV.encode()] = b"1"
    return env


def pytest_configure(config):
    """Before collection, re-exec guard-visible pytest as a harmless waiter."""
    if (
        os.environ.get(CHECKED_ENV) == "1"
        or os.environ.get(LEGACY_CHECKED_ENV) == "1"
        or os.environ.get("PYTEST_XDIST_WORKER")
    ):
        return
    os.environ[CHECKED_ENV] = "1"
    os.environ[LEGACY_CHECKED_ENV] = "1"
    if _is_bypassed() or not live_markers() or _exempt():
        return
    try:
        argv = own_cmdline()
        reason = guard.classify_command(argv) or ""
        if reason.startswith("pytest running"):
            capture = config.pluginmanager.getplugin("capturemanager")
            if capture:
                capture.stop_global_capturing()
            sys.stdout.flush()
            sys.stderr.flush()
            os.execve(
                sys.executable,
                [sys.executable, str(Path(__file__).resolve()), "wait", "--exec", sys.executable, "--", *argv],
                launch_environ(),
            )
        else:
            wait_for_clear()
    except Exception as exc:
        _log(f"pending-deploy check failed, proceeding: {type(exc).__name__}: {exc}")


@hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Request a controlled stop only after fixtures have completed teardown."""
    yield
    if call.when != "teardown" or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if _is_bypassed() or _exempt():
        return
    if live_markers():
        item.session.shouldstop = "deploy pending; yielding after completed test (retry this suite)"


def _cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("wait", "status"))
    parser.add_argument("--max-wait", type=float)
    parser.add_argument("--exec", dest="exec_path")
    options, rest = parser.parse_known_args(argv)
    if options.command == "status":
        print(describe(live_markers()) or "no deploy pending")
        return 0
    try:
        wait_for_clear(max_wait=options.max_wait)
    except Exception as exc:
        _log(f"wait failed, proceeding: {exc}")
    if options.exec_path:
        rest = rest[1:] if rest[:1] == ["--"] else rest
        if not rest:
            return 2
        os.environ[CHECKED_ENV] = "1"
        os.environ[LEGACY_CHECKED_ENV] = "1"
        os.execv(options.exec_path, rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
