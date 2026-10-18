from __future__ import annotations

from git_zmarshal.xcode import (
    canonical_xcode_lane,
    device_kind_for_target,
    merge_xcode_result,
    normalise_xcode_train,
    plan_xcode_enqueue,
    xcode_build_lane,
)

SHA_1 = "1" * 40
SHA_2 = "2" * 40
SHA_3 = "3" * 40


def test_canonical_xcode_lane_groups_universal_targets() -> None:
    assert canonical_xcode_lane("ios", "install") == ("universal-app", "install")
    assert canonical_xcode_lane("ipad", "install") == ("universal-app", "install")
    assert canonical_xcode_lane("watch", "install") == ("universal-app", "install")
    assert xcode_build_lane("ios") == "universal-app"
    assert xcode_build_lane("ipad") == "universal-app"
    assert xcode_build_lane("watch") == "universal-app"

    assert canonical_xcode_lane("ios", "compile-check") == ("universal-app", "compile-check")
    assert canonical_xcode_lane("ipad", "compile-check") == ("universal-app", "compile-check")
    assert canonical_xcode_lane("ios", "simulator-test") == ("universal-app", "simulator-test")
    assert canonical_xcode_lane("ipad", "ota") == ("universal-app", "ota")

    # Watch simulator gate uses its own watchOS Simulator scheme -> separate lane
    assert canonical_xcode_lane("watch", "compile-check") == ("watch", "compile-check")
    # Distinct apps keep their own lanes
    assert canonical_xcode_lane("companion-glasses", "install") == ("companion-glasses", "install")
    assert canonical_xcode_lane("ar-viewer", "install") == ("ar-viewer", "install")
    assert canonical_xcode_lane("mesh-engine", "device-test") == ("mesh-engine", "device-test")

    # Custom universal lane name is honoured
    assert xcode_build_lane("ios", universal_lane="flagship-ios") == "flagship-ios"


def test_device_kind_for_target() -> None:
    assert device_kind_for_target("ios", "install") == "iphone"
    assert device_kind_for_target("watch", "install") == "iphone"
    assert device_kind_for_target("ipad", "install") == "ipad"
    assert device_kind_for_target("ios", "compile-check") is None
    assert device_kind_for_target("mesh-engine", "install") is None
    assert (
        device_kind_for_target(
            "companion-glasses",
            "install",
            iphone_targets={"ios", "watch", "companion-glasses"},
        )
        == "iphone"
    )


def test_plan_xcode_enqueue_coalesces_queued_wave_and_fans_out_devices() -> None:
    # 1. First request starts a new queued wave
    p1 = plan_xcode_enqueue(
        [],
        {
            "target": "ios",
            "job_type": "install",
            "git_ref": "main",
            "reason": "commit 1",
            "requested_by": "agent-a",
            "launch": False,
            "expected_revision": SHA_1,
            "idempotency_key": "k1",
        },
    )
    assert p1.action == "insert"
    assert p1.train_step == "started"
    assert p1.coalesced is False
    assert p1.launch is False
    assert p1.train["coalesced_count"] == 1
    assert p1.train["device_kinds"] == ["iphone"]
    assert p1.train["expected_revisions"] == [SHA_1]

    queued_job = {
        "id": "job-1",
        "status": "queued",
        "target": "ios",
        "job_type": "install",
        "git_ref": "main",
        "launch": p1.launch,
        "result": {"train": p1.train},
    }

    # 2. Second request for iPad on origin/main coalesces into the same universal wave (using kwargs!)
    p2 = plan_xcode_enqueue(
        [queued_job],
        target="ipad",
        job_type="install",
        git_ref="origin/main",
        reason="commit 2 + ipad",
        actor="agent-b",
        launch=True,
        expected_revision=SHA_2,
        idempotency_key="k2",
    )
    assert p2.action == "join_queued"
    assert p2.train_step == "joined_queued"
    assert p2.coalesced is True
    assert p2.launch is True
    assert p2.train["coalesced_count"] == 2
    assert p2.train["requested_targets"] == ["ios", "ipad"]
    assert p2.train["device_kinds"] == ["iphone", "ipad"]
    assert p2.train["expected_revisions"] == [SHA_1, SHA_2]
    assert p2.train["coalesced_idempotency_keys"] == ["k1", "k2"]

    queued_job["launch"] = p2.launch
    queued_job["result"] = {"train": p2.train}

    # 3. Retry with idempotency_key="k2" deduplicates without incrementing coalesced_count
    p_dup = plan_xcode_enqueue(
        [queued_job],
        {
            "target": "ipad",
            "job_type": "install",
            "git_ref": "main",
            "idempotency_key": "k2",
        },
    )
    assert p_dup.action == "deduplicated"
    assert p_dup.train_step == "deduplicated"
    assert p_dup.coalesced is True
    assert p_dup.train["coalesced_count"] == 2


def test_plan_xcode_enqueue_joins_claimed_when_sha_matches_or_queues_next_on_newer_sha() -> None:
    claimed_job = {
        "id": "job-claimed-1",
        "status": "claimed",
        "target": "ios",
        "job_type": "install",
        "git_ref": "main",
        "launch": True,
        "result": {
            "train": normalise_xcode_train(
                {
                    "target": "ios",
                    "job_type": "install",
                    "git_ref": "main",
                    "launch": True,
                    "expected_revision": SHA_1,
                }
            )
        },
    }

    # Same SHA_1 + compatible target ('watch' installs via iphone host) joins claimed job
    join_plan = plan_xcode_enqueue(
        [claimed_job],
        {
            "target": "watch",
            "job_type": "install",
            "git_ref": "main",
            "launch": False,
            "expected_revision": SHA_1,
        },
    )
    assert join_plan.action == "join_claimed"
    assert join_plan.train_step == "joined_running"
    assert join_plan.coalesced is True

    # Newer SHA_2 does NOT join the claimed job -> queues next wave ("queued_next")
    next_plan = plan_xcode_enqueue(
        [claimed_job],
        {
            "target": "ios",
            "job_type": "install",
            "git_ref": "main",
            "launch": False,
            "expected_revision": SHA_2,
        },
    )
    assert next_plan.action == "insert"
    assert next_plan.train_step == "queued_next"
    assert next_plan.coalesced is False

    # Subsequent SHA_3 while job-claimed-1 is still running coalesces into the single queued_next wave
    queued_next_job = {
        "id": "job-queued-2",
        "status": "queued",
        "target": "ios",
        "job_type": "install",
        "git_ref": "main",
        "launch": False,
        "result": {"train": next_plan.train},
    }
    p3 = plan_xcode_enqueue(
        [claimed_job, queued_next_job],
        {
            "target": "ipad",
            "job_type": "install",
            "git_ref": "main",
            "launch": True,
            "expected_revision": SHA_3,
        },
    )
    assert p3.action == "join_queued"
    assert p3.target_job is not None and p3.target_job["id"] == "job-queued-2"
    assert p3.train["expected_revisions"] == [SHA_2, SHA_3]
    assert p3.train["device_kinds"] == ["iphone", "ipad"]


def test_merge_xcode_result_preserves_train_and_stamps_resolved_commit() -> None:
    existing = {
        "train": {
            "lane": ["universal-app", "install"],
            "coalesced_count": 3,
            "device_kinds": ["iphone", "ipad"],
            "expected_revisions": [SHA_1, SHA_2],
        }
    }
    reported = {
        "log_tail": "SUCCESS",
        "target": "ios",
        "job_type": "install",
        "commit": SHA_2,
        "installed_devices": ["iphone", "ipad"],
        "installed_udids": ["UDID-1", "UDID-2"],
    }
    merged = merge_xcode_result(existing, reported)
    assert merged["commit"] == SHA_2
    assert merged["log_tail"] == "SUCCESS"
    assert merged["train"]["coalesced_count"] == 3
    assert merged["train"]["device_kinds"] == ["iphone", "ipad"]
    assert merged["train"]["resolved_commit"] == SHA_2
    assert merged["train"]["installed_devices"] == ["iphone", "ipad"]
    assert merged["train"]["installed_udids"] == ["UDID-1", "UDID-2"]


def test_normalise_git_ref_strips_remote_and_heads_prefixes() -> None:
    from git_zmarshal.xcode import normalise_git_ref

    assert normalise_git_ref(None) == "main"
    assert normalise_git_ref("") == "main"
    assert normalise_git_ref("HEAD") == "main"
    assert normalise_git_ref("origin/main") == "main"
    assert normalise_git_ref("refs/heads/main") == "main"
    assert normalise_git_ref("refs/remotes/origin/main") == "main"
    assert normalise_git_ref("origin/feature/x") == "feature/x"
    assert normalise_git_ref("refs/heads/feature/x") == "feature/x"
    assert normalise_git_ref("refs/remotes/origin/feature/x") == "feature/x"
    assert normalise_git_ref("ABCDEF1234567890ABCDEF1234567890ABCDEF12") == (
        "abcdef1234567890abcdef1234567890abcdef12"
    )


def test_claimed_job_authoritative_commit_and_stale_claim_filtering() -> None:
    claimed = {
        "id": "job-claimed-actual-sha2",
        "status": "claimed",
        "target": "ios",
        "job_type": "install",
        "git_ref": "main",
        "claimed_at": "2026-10-18T00:50:00Z",
        "result": {
            "commit": SHA_2,
            "train": normalise_xcode_train(
                {
                    "target": "ios",
                    "job_type": "install",
                    "git_ref": "main",
                    "expected_revision": SHA_1,
                }
            ),
        },
    }
    # Caller expecting SHA_1 must NOT join because the worker actually checked out SHA_2
    p_mismatch = plan_xcode_enqueue(
        [claimed],
        {"target": "ios", "job_type": "install", "git_ref": "main", "expected_revision": SHA_1},
        now="2026-10-18T01:00:00Z",
    )
    assert p_mismatch.action == "insert"
    assert p_mismatch.train_step == "queued_next"

    # Caller expecting SHA_2 joins the claimed job
    p_match = plan_xcode_enqueue(
        [claimed],
        {"target": "ios", "job_type": "install", "git_ref": "main", "expected_revision": SHA_2},
        now="2026-10-18T01:00:00Z",
    )
    assert p_match.action == "join_claimed"

    # Once claimed_at is >45m old, the stale claimed job is ignored completely
    p_stale = plan_xcode_enqueue(
        [claimed],
        {"target": "ios", "job_type": "install", "git_ref": "main", "expected_revision": SHA_2},
        now="2026-10-18T02:00:00Z",
    )
    assert p_stale.action == "insert"
    assert p_stale.train_step == "started"


def test_no_coalesce_marks_job_isolated_and_prevents_later_joins() -> None:
    p_iso = plan_xcode_enqueue(
        [],
        {
            "target": "ios",
            "job_type": "install",
            "git_ref": "main",
            "coalesce": False,
        },
    )
    assert p_iso.action == "insert"
    assert p_iso.train.get("isolated") is True

    isolated_job = {
        "id": "job-iso-1",
        "status": "queued",
        "target": "ios",
        "job_type": "install",
        "git_ref": "main",
        "result": {"train": p_iso.train},
    }
    p_next = plan_xcode_enqueue(
        [isolated_job],
        {
            "target": "ipad",
            "job_type": "install",
            "git_ref": "main",
            "coalesce": True,
        },
    )
    assert p_next.action == "insert"
    assert p_next.coalesced is False
