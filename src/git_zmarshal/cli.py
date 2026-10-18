"""Command-line interface for ``git-zmarshal`` (also invocable as ``git zmarshal``)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import __version__, guard, train, wave, xcode


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_demo(*, as_json: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="git-zmarshal-demo-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "--initial-branch=main")
        _git(repo, "config", "user.name", "Z-Marshal Demo")
        _git(repo, "config", "user.email", "demo@example.invalid")

        agents = [
            ("agent-alpha", "feat(auth): add token validation"),
            ("agent-bravo", "fix(api): bound retry backoff"),
            ("agent-charlie", "perf(db): index session lookup"),
            ("agent-delta", "feat(ui): responsive iPad split view"),
            ("agent-echo", "chore(ci): tighten ancestry check"),
        ]
        commits: list[dict[str, str]] = []
        for actor, subject in agents:
            _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", subject)
            sha = _git(repo, "rev-parse", "HEAD")
            commits.append({"actor": actor, "subject": subject, "sha": sha})

        # Create a side branch commit that was NOT merged into main
        _git(repo, "checkout", "-q", "-b", "feature/unmerged", commits[0]["sha"])
        _git(
            repo,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-qm",
            "wip: experimental branch",
        )
        unmerged_sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")

        # 1. Server Deploy Train Coalescing
        policy = train.DeployTrainPolicy(window_seconds=5.0, request_limit=200)
        wave_1 = {
            "job_id": "wave-1",
            "action": "deploy",
            "status": "running",
            "actor": commits[0]["actor"],
            "reason": commits[0]["subject"],
            "idempotency_key": "req-1",
            "source_revision": commits[0]["sha"],
        }
        wave_2 = {
            "job_id": "wave-2",
            "action": "deploy",
            "status": "queued",
            "actor": commits[1]["actor"],
            "reason": commits[1]["subject"],
            "idempotency_key": "req-2",
            "source_revision": commits[1]["sha"],
        }
        for idx, item in enumerate(commits[2:], start=3):
            wave_2 = train.add_train_request(
                wave_2,
                actor=item["actor"],
                reason=item["subject"],
                idempotency_key=f"req-{idx}",
                revision=item["sha"],
            )
        # Replay req-3 to verify idempotency deduplication
        wave_2 = train.add_train_request(
            wave_2,
            actor=commits[2]["actor"],
            reason=commits[2]["subject"],
            idempotency_key="req-3",
            revision=commits[2]["sha"],
        )

        # 2. Universal Xcode Build Train Coalescing
        xcode_plan_1 = xcode.plan_xcode_enqueue(
            [],
            target="ios",
            job_type="install",
            git_ref="main",
            actor="agent-alpha",
            reason="verify iPhone build",
            expected_revision=commits[1]["sha"],
            idempotency_key="xc-1",
        )
        queued_xcode_job = {
            "id": "xcode-wave-1",
            "status": "queued",
            "target": "ios",
            "job_type": "install",
            "git_ref": "main",
            "launch": xcode_plan_1.launch,
            "result": {"train": xcode_plan_1.train},
        }
        xcode_plan_2 = xcode.plan_xcode_enqueue(
            [queued_xcode_job],
            target="ipad",
            job_type="install",
            git_ref="origin/main",
            actor="agent-delta",
            reason="verify iPad split view",
            launch=True,
            expected_revision=commits[3]["sha"],
            idempotency_key="xc-2",
        )
        queued_xcode_job["launch"] = xcode_plan_2.launch
        queued_xcode_job["result"] = {"train": xcode_plan_2.train}
        xcode_plan_3 = xcode.plan_xcode_enqueue(
            [queued_xcode_job],
            target="watch",
            job_type="install",
            git_ref="refs/heads/main",
            actor="agent-echo",
            reason="verify watch companion",
            expected_revision=commits[4]["sha"],
            idempotency_key="xc-3",
        )

        # 3. Host Admission Guard & Pending Marker Simulation
        fake_proc = root / "proc"
        (fake_proc / "101").mkdir(parents=True)
        (fake_proc / "101" / "cmdline").write_bytes(b"python3\x00-m\x00pytest\x00-q\x00")
        (fake_proc / "101" / "cgroup").write_text("0::/system.slice/docker-0123456789abcdef.scope\n")
        (fake_proc / "202").mkdir(parents=True)
        (fake_proc / "202" / "cmdline").write_bytes(b"python3\x00-m\x00pytest\x00tests/\x00")
        (fake_proc / "202" / "cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")

        verdict = guard.HostAdmissionGuard(guard.GuardConfig(proc_root=fake_proc)).check()
        pending_store = guard.PendingStore(root / "pending")
        marker = pending_store.record(
            "api",
            {"action": "deploy"},
            actor="marshaling-yard",
            holds=verdict.holds,
            owner_id="wave-2",
        )

        # 4. Multi-Commit Ancestry Proof
        deployed_sha = wave_2["source_revision"]
        expected_shas = [c["sha"] for c in commits]
        proof = wave.verify_wave(repo, expected_shas, deployed_sha)
        negative_proof = wave.verify_wave(repo, [unmerged_sha], deployed_sha)

        summary = {
            "merges_total": len(commits),
            "deploy_waves_dispatched": 2,
            "wave_1": {
                "job_id": wave_1["job_id"],
                "deployed_commit": wave_1["source_revision"][:12],
                "coalesced_count": 1,
            },
            "wave_2": {
                "job_id": wave_2["job_id"],
                "deployed_commit": deployed_sha[:12],
                "coalesced_count": wave_2["coalesced_count"],
                "coalesced_actors": wave_2["coalesced_actors"],
                "idempotency_keys": wave_2["coalesced_idempotency_keys"],
            },
            "xcode_wave": {
                "lane": xcode_plan_3.train["lane"],
                "coalesced_count": xcode_plan_3.train["coalesced_count"],
                "requested_targets": xcode_plan_3.train["requested_targets"],
                "device_kinds": xcode_plan_3.train["device_kinds"],
                "launch": xcode_plan_3.launch,
            },
            "admission_guard": {
                "ok": verdict.ok,
                "holds": verdict.holds,
                "pending_marker_owner": marker.get("owner_id"),
            },
            "ancestry_proof": {
                "deployed_commit": (proof.live_commit or "")[:12],
                "all_merged_commits_verified": proof.ok,
                "statuses": [
                    {"commit": r.revision[:12], "status": r.status} for r in proof.results
                ],
                "unmerged_branch_status": negative_proof.results[0].status,
            },
            "can_share_running_same_sha": policy.can_share_running(wave_1, commits[0]["sha"]),
            "can_share_running_newer_sha": policy.can_share_running(wave_1, commits[1]["sha"]),
        }

        if as_json:
            print(json.dumps(summary, indent=2))
            return 0

        print("git-zmarshal demo — Z-scale Git deploy & build marshaling yard")
        print("=" * 64)
        print(f"1. Simulated {len(commits)} rapid agent merges on 'main' -> 2 deploy waves:")
        print(
            f"   - Wave #1 (running): {commits[0]['sha'][:12]} ({commits[0]['actor']}: {commits[0]['subject']})"
        )
        print(
            f"   - Wave #2 (queued):  {deployed_sha[:12]} coalesced {wave_2['coalesced_count']} merges "
            f"from {', '.join(wave_2['coalesced_actors'])} (retry req-3 deduplicated)"
        )
        print()
        print("2. Universal Xcode build marshaling (ios + ipad + watch on equivalent main refs):")
        print(
            f"   - Lane:              {xcode_plan_3.train['lane'][0]} ({xcode_plan_3.train['lane'][1]})"
        )
        print(f"   - Coalesced count:   {xcode_plan_3.train['coalesced_count']} requests -> 1 compile")
        print(f"   - Requested targets: {', '.join(xcode_plan_3.train['requested_targets'])}")
        print(f"   - Device fan-out:    {', '.join(xcode_plan_3.train['device_kinds'])}")
        print()
        print("3. Host admission guard (/proc contender check + container cgroup exemption):")
        print("   - PID 101 (pytest in docker-*.scope): EXEMPT")
        print(f"   - PID 202 (host pytest):              HELD ({', '.join(verdict.holds)})")
        print(
            f"   - Pending marker recorded for service 'api' (owner_id={marker.get('owner_id')})"
        )
        print()
        print("4. Multi-commit Git ancestry proof (git merge-base --is-ancestor):")
        for item, res in zip(commits, proof.results):
            print(f"   - {res.revision[:12]} ({item['actor']}): {res.status.upper()}")
        print(
            f"   - {unmerged_sha[:12]} (unmerged side branch): {negative_proof.results[0].status.upper()}"
        )
        print("=" * 64)
        print(
            f"Result: {len(commits)} merges verified in { deployed_sha[:12] } (proof.ok={proof.ok})."
        )
        return 0 if proof.ok and not negative_proof.ok else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    proof = wave.verify_wave(
        args.repo,
        args.expected_revisions,
        args.live_revision,
        timeout=args.timeout,
    )
    payload = {
        "ok": proof.ok,
        "unknown": proof.unknown,
        "live_revision": proof.live_revision,
        "live_commit": proof.live_commit,
        "results": [
            {
                "revision": r.revision,
                "commit": r.commit,
                "status": r.status,
                "ok": r.ok,
                "detail": r.detail,
            }
            for r in proof.results
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for r in proof.results:
            commit_short = (r.commit or "unresolved")[:12]
            suffix = f" ({r.detail})" if r.detail else ""
            print(f"{r.revision} -> {commit_short}: {r.status}{suffix}")
    return 0 if proof.ok else 1


def _cmd_resolve(args: argparse.Namespace) -> int:
    try:
        resolved = wave.resolve_revisions(args.repo, args.revisions, timeout=args.timeout)
    except (wave.GitVerificationError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, "commits": list(resolved)}, indent=2))
    else:
        for sha in resolved:
            print(sha)
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    cfg = guard.GuardConfig(
        proc_root=Path(args.proc_root),
        max_load_per_cpu=args.max_load_per_cpu,
        max_swap_used_ratio=args.max_swap_ratio,
        pressure_limit=args.pressure_limit,
    )
    verdict = guard.HostAdmissionGuard(cfg).check()
    markers = []
    if args.pending_dir:
        markers = guard.PendingStore(args.pending_dir).markers()[0]
    payload = {
        "ok": verdict.ok,
        "holds": verdict.holds,
        "detail": verdict.detail,
        "pending_markers": markers,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        status = "ADMIT" if verdict.ok else "HOLD"
        print(f"Host admission: {status}")
        for hold in verdict.holds:
            print(f"  - hold: {hold}")
        for m in markers:
            print(f"  - pending marker: service={m.get('service')} owner={m.get('owner_id')}")
    return 0 if verdict.ok else 1


def _cmd_plan_wave(args: argparse.Namespace) -> int:
    groups: dict[str, str] = {}
    for spec in args.group or []:
        if "=" not in spec:
            print(f"error: expected SERVICE=GROUP, got {spec!r}", file=sys.stderr)
            return 2
        svc, grp = spec.split("=", 1)
        groups[svc.strip()] = grp.strip()
    ordered = wave.plan_wave(args.services, groups)
    if args.json:
        print(json.dumps({"services": ordered}, indent=2))
    else:
        print(" ".join(ordered))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-zmarshal",
        description="Z-scale Git deploy & Xcode build marshaling yard for multi-agent coding hosts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    demo_p = sub.add_parser(
        "demo",
        help="Run a self-contained simulation of multi-agent commit coalescing and ancestry verification",
    )
    demo_p.add_argument("--json", action="store_true", help="Emit JSON output")

    verify_p = sub.add_parser(
        "verify",
        help="Verify that all expected revisions are ancestors of a deployed revision",
    )
    verify_p.add_argument("live_revision", help="Deployed/live commit or ref")
    verify_p.add_argument("expected_revisions", nargs="+", help="Expected commits or refs in the wave")
    verify_p.add_argument("--repo", default=".", help="Path to Git repository (default: current directory)")
    verify_p.add_argument("--timeout", type=float, default=10.0, help="Shared verification timeout in seconds")
    verify_p.add_argument("--json", action="store_true", help="Emit JSON output")

    resolve_p = sub.add_parser(
        "resolve",
        help="Freeze revision names to full immutable commit hashes",
    )
    resolve_p.add_argument("revisions", nargs="+", help="Revisions to resolve")
    resolve_p.add_argument("--repo", default=".", help="Path to Git repository (default: current directory)")
    resolve_p.add_argument("--timeout", type=float, default=10.0, help="Shared resolution timeout in seconds")
    resolve_p.add_argument("--json", action="store_true", help="Emit JSON output")

    guard_p = sub.add_parser(
        "guard",
        help="Check host admission (/proc contenders, load, swap, PSI) and pending markers",
    )
    guard_p.add_argument("--proc-root", default="/proc", help="Path to procfs (default: /proc)")
    guard_p.add_argument("--pending-dir", default=None, help="Optional pending marker directory to inspect")
    guard_p.add_argument("--max-load-per-cpu", type=float, default=None)
    guard_p.add_argument("--max-swap-ratio", type=float, default=None)
    guard_p.add_argument("--pressure-limit", type=float, default=None)
    guard_p.add_argument("--json", action="store_true", help="Emit JSON output")

    plan_p = sub.add_parser(
        "plan-wave",
        help="Deduplicate services and interleave lock groups",
    )
    plan_p.add_argument("services", nargs="+", help="Services to schedule in the wave")
    plan_p.add_argument(
        "--group",
        action="append",
        metavar="SERVICE=GROUP",
        help="Assign a service to a shared lock group",
    )
    plan_p.add_argument("--json", action="store_true", help="Emit JSON output")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "demo"):
        return _run_demo(as_json=getattr(args, "json", False))
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "resolve":
        return _cmd_resolve(args)
    if args.command == "guard":
        return _cmd_guard(args)
    if args.command == "plan-wave":
        return _cmd_plan_wave(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
