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
