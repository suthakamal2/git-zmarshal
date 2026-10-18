"""Tests for git_zmarshal.cli, README examples, and backwards-compatible git_deploy_train imports."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from git_zmarshal import cli


def test_cli_demo_text_and_json(capsys):
    rc = cli.main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "git-zmarshal demo" in out
    assert "coalesced 4 merges" in out

    rc_json = cli.main(["demo", "--json"])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["merges_total"] == 5
    assert payload["deploy_waves_dispatched"] == 2
    assert payload["wave_2"]["coalesced_count"] == 4
    assert payload["xcode_wave"]["coalesced_count"] == 3
    assert payload["xcode_wave"]["device_kinds"] == ["iphone", "ipad"]
    assert payload["ancestry_proof"]["all_merged_commits_verified"] is True
    assert payload["ancestry_proof"]["unmerged_branch_status"] == "not-ancestor"


def test_cli_verify_resolve_guard_and_plan_wave(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "--initial-branch=main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    shas = []
    for msg in ("c1", "c2"):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", msg],
            check=True,
        )
        shas.append(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

    assert cli.main(["resolve", "main", "--repo", str(repo), "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved == {"ok": True, "commits": [shas[-1]]}

    assert cli.main(["verify", shas[-1], shas[0], shas[1], "--repo", str(repo), "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True

    proc = tmp_path / "proc"
    proc.mkdir()
    assert cli.main(["guard", "--proc-root", str(proc), "--json"]) == 0
    guard_out = json.loads(capsys.readouterr().out)
    assert guard_out["ok"] is True

    assert cli.main(["plan-wave", "api", "worker", "docs", "--group", "api=core", "--group", "worker=core", "--json"]) == 0
    plan_out = json.loads(capsys.readouterr().out)
    assert plan_out["services"] == ["api", "docs", "worker"]


def test_backwards_compatible_git_deploy_train_imports():
    import git_deploy_train
    from git_deploy_train import guard, pytest_plugin, train, wave, xcode

    assert git_deploy_train.__version__ == "0.1.0"
    assert train.DeployTrainPolicy().eligible(action="deploy")
    assert guard.PendingStore
    assert pytest_plugin.wait_for_clear
    assert wave.plan_wave(["a", "b"]) == ["a", "b"]
    assert xcode.xcode_build_lane("ios") == "universal-app"


def test_readme_code_examples_execute_cleanly():
    """Execute the standalone Python blocks in README.md that do not require /srv/repo."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in readme.splitlines():
        if line.strip() == "```python":
            in_block = True
            current = []
        elif in_block and line.strip() == "```":
            in_block = False
            blocks.append("\n".join(current))
        elif in_block:
            current.append(line)

    assert len(blocks) >= 4
    executed = 0
    for code in blocks:
        if "/srv/repo" in code:
            continue
        namespace: dict = {}
        exec(code, namespace)
        executed += 1
    assert executed >= 4


def test_minimal_yard_sqlite_runner_coalesces_holds_and_verifies(tmp_path: Path):
    import importlib.util
    import sys

    from git_zmarshal.guard import GuardConfig, HostAdmissionGuard

    example_path = Path(__file__).resolve().parents[1] / "examples" / "minimal_yard.py"
    spec = importlib.util.spec_from_file_location("minimal_yard", example_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "--initial-branch=main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)

    shas: list[str] = []
    for i in range(1, 4):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", f"c{i}"],
            check=True,
        )
        shas.append(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

    proc = tmp_path / "proc"
    (proc / "500").mkdir(parents=True)
    (proc / "500" / "cmdline").write_bytes(b"python3\x00-m\x00pytest\x00")
    (proc / "500" / "cgroup").write_text("0::/user.slice/session-1.scope\n")

    yard = mod.MinimalYard(
        tmp_path / "yard.db",
        repo,
        pending_dir=tmp_path / "pending",
        guard=HostAdmissionGuard(GuardConfig(proc_root=proc)),
    )

    w1 = yard.enqueue("api", actor="a1", reason="r1", revision=shas[0], idempotency_key="k1")
    w2 = yard.enqueue("api", actor="a2", reason="r2", revision=shas[1], idempotency_key="k2")
    w3 = yard.enqueue("api", actor="a3", reason="r3", revision=shas[2], idempotency_key="k3")
    assert w1["step"] == "started"
    assert w2["step"] == "joined_queued" and w2["job_id"] == w1["job_id"]
    assert w3["step"] == "joined_queued" and w3["coalesced_count"] == 3

    # Held while host pytest contender is active; records owner-tagged pending marker
    held = yard.step("api", [sys.executable, "-c", "pass"])
    assert held is not None and held["status"] == "held"
    assert yard.pending.read("api")["owner_id"] == w1["job_id"]

    # Contender finishes -> wave departs, clears pending marker, and proves all 3 commits
    (proc / "500" / "cmdline").unlink()
    ran = yard.step("api", [sys.executable, "-c", "pass"])
    assert ran is not None and ran["status"] == "succeeded"
    assert yard.pending.read("api") is None

    proof = yard.verify(w1["job_id"])
    assert proof["ok"] is True
    assert [r["status"] for r in proof["results"]] == ["ancestor", "ancestor", "equal"]

    # Idempotent retry after completion deduplicates against the completed SQLite row
    dup = yard.enqueue("api", actor="a2", reason="r2", revision=shas[1], idempotency_key="k2")
    assert dup["step"] == "deduplicated" and dup["job_id"] == w1["job_id"]


def test_shim_parity_and_isolated_loading():
    import sys

    root = Path(__file__).resolve().parents[1]
    zm_dir = root / "src" / "git_zmarshal"
    dt_dir = root / "src" / "git_deploy_train"
    for name in ("__init__.py", "cli.py", "guard.py", "py.typed", "pytest_plugin.py", "train.py", "wave.py", "xcode.py"):
        assert (zm_dir / name).read_bytes() == (dt_dir / name).read_bytes(), name

    code = (
        "import importlib.util, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "for pkg in ('git_zmarshal', 'git_deploy_train'):\n"
        "    for mod in ('guard', 'train', 'wave', 'xcode', 'pytest_plugin'):\n"
        "        p = root / 'src' / pkg / f'{mod}.py'\n"
        "        s = importlib.util.spec_from_file_location(f'_{pkg}_{mod}', p)\n"
        "        m = importlib.util.module_from_spec(s)\n"
        "        sys.modules[s.name] = m\n"
        "        s.loader.exec_module(m)\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(root)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr



