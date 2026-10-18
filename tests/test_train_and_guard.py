"""Standalone unit and cross-process tests for git_zmarshal.train, guard, and pytest_plugin."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from git_zmarshal import guard, pytest_plugin, train


def test_train_policy_can_share_running_only_when_revisions_match():
    policy = train.DeployTrainPolicy(enabled=True, window_seconds=5.0)
    assert policy.eligible(action="deploy")
    assert not policy.eligible(action="rollback")
    assert not policy.eligible(action="deploy", force=True)
    assert not policy.eligible(action="deploy", fresh=True)
    assert not policy.eligible(action="deploy", expected_revision="a" * 40)

    running = {"action": "deploy", "status": "running", "source_revision": "a" * 40}
    assert policy.can_share_running(running, "a" * 40)
    assert not policy.can_share_running(running, "b" * 40)
    assert not policy.can_share_running(running, None)
    assert not policy.can_share_running({**running, "status": "accepted"}, "a" * 40)


def test_add_train_request_deduplicates_idempotency_keys_and_tracks_max_probe():
    job = {
        "action": "deploy",
        "status": "queued",
        "actor": "agent-1",
        "reason": "merge 1",
        "idempotency_key": "key-1",
        "source_revision": "a" * 40,
        "probe_seconds": 30,
    }
    updated = train.add_train_request(
        job,
        actor="agent-2",
        reason="merge 2",
        idempotency_key="key-2",
        revision="b" * 40,
        probe_seconds=120,
    )
    assert updated["coalesced_count"] == 2
    assert updated["coalesced_idempotency_keys"] == ["key-1", "key-2"]
    assert updated["source_revision"] == "b" * 40
    assert updated["probe_seconds"] == 120

    replay = train.add_train_request(
        updated,
        actor="agent-2",
        reason="merge 2",
        idempotency_key="key-2",
        revision="b" * 40,
    )
    assert replay["coalesced_count"] == 2


def test_host_admission_guard_exempts_container_cgroups(tmp_path: Path):
    proc = tmp_path / "proc"
    (proc / "101").mkdir(parents=True)
    (proc / "101" / "cmdline").write_bytes(b"python3\x00-m\x00pytest\x00-q\x00")
    (proc / "101" / "cgroup").write_text("0::/system.slice/docker-0123456789abcdef.scope\n")

    verdict = guard.HostAdmissionGuard(guard.GuardConfig(proc_root=proc)).check()
    assert verdict.ok
    assert verdict.holds == []

    (proc / "102").mkdir(parents=True)
    (proc / "102" / "cmdline").write_bytes(b"python3\x00-m\x00pytest\x00-q\x00")
    (proc / "102" / "cgroup").write_text("0::/user.slice/user-1000.slice/session-1.scope\n")

    verdict2 = guard.HostAdmissionGuard(guard.GuardConfig(proc_root=proc)).check()
    assert not verdict2.ok
    assert len(verdict2.holds) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/system.slice/docker-abc123.scope",
        "/docker/abc123",
        "/containerd/abc123",
        "/kubepods/besteffort/pod123",
        "/kubepods.slice/kubepods-burstable.slice/pod123",
        "/system.slice/cri-containerd-aabb.scope",
        "/actions_job/test123",
        "/actions_job-12.scope",
    ],
)
def test_container_cgroups_exempt_pytest(path):
    assert guard.pytest_cgroup_exempt(f"0::{path}\n")


@pytest.mark.parametrize(
    "path",
    [
        "/user.slice/not-containerd",
        "/system.slice/docker-pretend.service",
        "/system.slice/docker-",
        "/user.slice/not_actions_job",
        "/user.slice/kubepods-lookalike",
    ],
)
def test_cgroup_lookalikes_do_not_exempt_host_tests(path):
    assert not guard.pytest_cgroup_exempt(f"0::{path}\n")


@pytest.mark.parametrize(
    "argv",
    [
        ["python3.14", "-W", "ignore", "-m", "pytest"],
        ["python3", "-X", "dev", "-m", "pytest"],
        ["python", "--", "/venv/bin/pytest"],
    ],
)
def test_interpreter_value_options_do_not_hide_pytest(argv):
    assert guard.classify_command(argv).startswith("pytest running")


def test_python_code_named_pytest_is_not_an_entrypoint():
    assert guard.classify_command(["python", "-c", "pytest"]) is None
    assert guard.classify_command(["python", "-cprint(1)", "pytest"]) is None
    assert guard.classify_command(["python", "-mpytest"]).startswith("pytest running")


def test_bare_pytest_entrypoint_is_a_contender():
    assert guard.classify_command(["/venv/bin/pytest", "-q"]).startswith("pytest running")


@pytest.mark.parametrize(
    "options",
    [
        {"cpu_count": 0},
        {"cpu_count": -1},
        {"cpu_count": True},
        {"max_load_per_cpu": float("nan")},
        {"max_load_per_cpu": float("inf")},
        {"max_swap_used_ratio": 1.1},
        {"pressure_limit": -1},
    ],
)
def test_invalid_guard_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        guard.GuardConfig(**options)


def test_host_load_and_swap_limits_use_injected_proc(tmp_path):
    (tmp_path / "loadavg").write_text("6 2 1 2/3 42\n")
    (tmp_path / "stat").write_text("cpu 1 2 3\ncpu0 1 2 3\ncpu1 1 2 3\n")
    (tmp_path / "meminfo").write_text("SwapTotal: 1000 kB\nSwapFree: 100 kB\n")
    config = guard.GuardConfig(proc_root=tmp_path, max_load_per_cpu=2, max_swap_used_ratio=0.8)
    result = guard.HostAdmissionGuard(config).check()
    assert not result.ok and len(result.holds) == 2
    assert result.detail["load_per_cpu"] == 3
    assert result.detail["swap_used_ratio"] == 0.9


def test_missing_host_metrics_fail_open(tmp_path):
    result = guard.HostAdmissionGuard(
        guard.GuardConfig(proc_root=tmp_path, max_load_per_cpu=1, max_swap_used_ratio=0.1)
    ).check()
    assert result.ok


def _refresh_from_process(directory, count):
    store = guard.PendingStore(directory)
    for _ in range(count):
        store.record("api", {"service": "api"})


def test_pending_refresh_does_not_lose_cross_process_updates(tmp_path):
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_refresh_from_process, args=(tmp_path, 8)) for _ in range(4)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join()
    assert guard.PendingStore(tmp_path).read("api")["denials"] == 32
    assert list(tmp_path.glob("*.tmp")) == []


def test_pending_store_and_flag_compare_and_clear(tmp_path: Path):
    store = guard.PendingStore(tmp_path / "pending")
    rec1 = store.record("api", {"action": "deploy"}, actor="ci", holds=["pytest"], owner_id="w1")
    rec2 = store.record("api", {"action": "deploy"}, actor="ci", holds=["pytest"], owner_id="w2")
    assert rec1["marker_id"] != rec2["marker_id"]
    assert not store.clear("api", owner_id="w1")
    assert store.read("api")["owner_id"] == "w2"
    assert store.clear("api", owner_id="w2")
    assert store.read("api") is None

    flag = guard.PendingFlag(tmp_path / "deploy-pending.json")
    flag.record("job-1")
    flag.record("job-2")
    assert not flag.clear("job-1")
    assert flag.read()["job_id"] == "job-2"
    assert flag.clear("job-2")
    assert flag.read() is None


def test_broken_denials_counter_can_be_refreshed(tmp_path):
    store = guard.PendingStore(tmp_path)
    record = store.record("api")
    record["denials"] = {"malformed": True}
    (tmp_path / "api.json").write_text(json.dumps(record))
    assert store.record("api")["denials"] == 1


def test_legacy_snapshot_clear_keeps_a_new_versioned_marker(tmp_path):
    store = guard.PendingStore(tmp_path)
    current = store.record("api")
    assert not store.clear("api", marker_id=None)
    del current["marker_id"]
    (tmp_path / "api.json").write_text(json.dumps(current))
    assert store.clear("api", marker_id=None)


@pytest.mark.parametrize("operation", ["record", "read", "clear"])
def test_every_store_path_operation_validates_service(tmp_path, operation):
    with pytest.raises(ValueError):
        getattr(guard.PendingStore(tmp_path), operation)("../outside")


def test_pytest_plugin_wait_for_clear_respects_bypass(monkeypatch):
    monkeypatch.setenv(pytest_plugin.BYPASS_ENV, "1")
    assert pytest_plugin.wait_for_clear(read_markers=lambda: [{"service": "api"}]) == "bypassed"


def test_pytest_plugin_wait_for_clear_respects_legacy_bypass(monkeypatch):
    monkeypatch.delenv(pytest_plugin.BYPASS_ENV, raising=False)
    monkeypatch.setenv(pytest_plugin.LEGACY_BYPASS_ENV, "1")
    assert pytest_plugin.wait_for_clear(read_markers=lambda: [{"service": "api"}]) == "bypassed"


def test_nonfinite_waits_are_bounded():
    assert pytest_plugin.bounded_wait(float("inf")) == 900
    assert pytest_plugin.bounded_wait(-1) == 0


def test_pytest_plugin_yields_after_teardown_with_nonzero_exit(tmp_path):
    """Exercise pytest's real hook dispatch; a partial suite must not pass."""
    package_src = Path(guard.__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_text(
        "import pytest\nfrom _pytest.reports import TestReport\n"
        "from git_zmarshal import pytest_plugin\n"
        "pytest_plugin._exempt = lambda: False\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    return TestReport.from_item_and_call(item, call)\n"
    )
    (tmp_path / "test_yield.py").write_text(
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "from git_zmarshal.guard import PendingStore\n"
        "@pytest.fixture\ndef resource():\n"
        "    yield\n    Path('teardown-ran').write_text('yes')\n"
        "def test_first(resource):\n"
        "    PendingStore(os.environ['GIT_ZMARSHAL_PENDING_DIR']).record('api')\n"
        "def test_second():\n    Path('second-ran').write_text('yes')\n"
    )
    environment = dict(
        os.environ,
        PYTHONPATH=str(package_src),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        GIT_ZMARSHAL_PENDING_DIR=str(tmp_path / "pending"),
    )
    environment.pop(pytest_plugin.BYPASS_ENV, None)
    environment.pop(pytest_plugin.LEGACY_BYPASS_ENV, None)
    environment.pop("PYTEST_XDIST_WORKER", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "git_zmarshal.pytest_plugin",
            "-c",
            os.devnull,
            str(tmp_path / "test_yield.py"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert (tmp_path / "teardown-ran").read_text() == "yes"
    assert not (tmp_path / "second-ran").exists()
    assert "yielding after completed test" in result.stdout


def test_guard_imports_under_isolated_python_without_site_packages():
    path = Path(guard.__file__).resolve()
    program = (
        "import importlib.util,sys; "
        f"s=importlib.util.spec_from_file_location('isolated_guard', {str(path)!r}); "
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);"
        "assert m.HostAdmissionGuard; assert m.PendingStore"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
