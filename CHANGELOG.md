# Changelog

All notable changes to `git-zmarshal` are documented in this file.

## [0.1.0] - 2026-10-18

### Added
- `git_zmarshal.train`: `DeployTrainPolicy`, `normalise_train`, and `add_train_request` for coalescing overlapping Git deployment requests with bounded audit history and full idempotency-key retention.
- `git_zmarshal.guard`: `HostAdmissionGuard`, `GuardConfig`, `PendingStore`, and `PendingFlag` for `/proc` contender classification, container cgroup exemptions (`docker`, `containerd`, `kubepods`, `actions_job`), load/swap/PSI checks, and atomic flock-serialised pending markers.
- `git_zmarshal.pytest_plugin`: opt-in cooperative `pytest` plugin (`-p git_zmarshal.pytest_plugin`) that waits as a non-contender before collection and yields after fixture teardown with interrupted status (`2`).
- `git_zmarshal.wave`: `plan_wave`, `resolve_revisions`, and fail-closed `verify_wave` multi-commit ancestry proofs (`git --no-replace-objects merge-base --is-ancestor`) with shallow-clone detection and shared deadline budgets.
- `git_zmarshal.xcode`: universal Apple/Xcode build-train coalescing (`xcode_build_lane`, `canonical_xcode_lane`, `normalise_git_ref`, `plan_xcode_enqueue`, `can_join_claimed_xcode_job`, `merge_xcode_result`) with multi-device install fan-out (`iphone`, `ipad`), stale-claim filtering, and isolation mode.
- `git_zmarshal.cli`: `git-zmarshal` / `git zmarshal` command-line tool (`demo`, `verify`, `resolve`, `guard`, `plan-wave`).
- Backwards-compatible `git_deploy_train` re-export alias package.
