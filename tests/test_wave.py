"""Portable tests: only Git, pytest and the installed package are required."""

import subprocess

import pytest

from git_zmarshal import wave


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "--initial-branch=main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    commits = []
    for subject in ("first", "second", "third"):
        git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", subject)
        commits.append(git(repo, "rev-parse", "HEAD"))
    git(repo, "checkout", "--detach", commits[0])
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "sibling")
    sibling = git(repo, "rev-parse", "HEAD")
    return repo, commits, sibling


def test_multiple_commits_must_all_be_ancestors(history):
    repo, (first, second, third), sibling = history
    result = wave.verify_wave(repo, [first, second, third, second], third)
    assert result.ok
    assert [item.status for item in result.results] == ["ancestor", "ancestor", "equal"]
    result = wave.verify_wave(repo, [first, sibling], third)
    assert not result.ok and not result.unknown
    assert [item.status for item in result.results] == ["ancestor", "not-ancestor"]


def test_matching_nonexistent_prefix_is_not_identity_proof(history):
    repo, _, _ = history
    result = wave.verify_wave(repo, ["deadbeef"], "deadbeef" + "0" * 32)
    assert not result.ok and result.unknown


def test_resolves_abbreviations_and_freezes_branch_names(history):
    repo, (_, second, third), _ = history
    assert wave.resolve_revisions(repo, [second[:12], "main"]) == (second, third)
    assert wave.verify_wave(repo, [second[:12]], "main").ok


def test_explicit_repository_cannot_be_overridden_by_git_hook_environment(history, tmp_path, monkeypatch):
    repo, (_, _, third), _ = history
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "--initial-branch=main")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert wave.resolve_revisions(repo, ["main"]) == (third,)


@pytest.mark.parametrize("revision", ["--help", "does-not-exist", "\0"])
def test_invalid_revision_is_unknown_not_a_git_option(history, revision):
    repo, commits, _ = history
    result = wave.verify_wave(repo, [revision], commits[-1])
    assert not result.ok and result.unknown


@pytest.mark.parametrize("revisions", [[], [""], "main"])
def test_empty_or_malformed_expectations_cannot_pass(history, revisions):
    repo, commits, _ = history
    with pytest.raises(ValueError):
        wave.verify_wave(repo, revisions, commits[-1])


def test_blob_is_not_a_commit(history):
    repo, commits, _ = history
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="blob", capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert wave.verify_wave(repo, [blob], commits[-1]).unknown


def test_shallow_negative_is_unknown_even_with_both_objects_present(history, tmp_path):
    repo, (first, _, third), _ = history
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", "--branch=main", repo.as_uri(), str(clone)],
        check=True, capture_output=True,
    )
    git(clone, "fetch", "-q", "--depth=1", "origin", first)
    assert git(clone, "cat-file", "-t", first) == "commit"
    result = wave.verify_wave(clone, [first], third)
    assert not result.ok and result.unknown
    assert "incomplete" in result.results[0].detail


def test_local_replacement_cannot_forge_ancestry(history):
    repo, (_, _, third), sibling = history
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "replacement")
    replacement = git(repo, "rev-parse", "HEAD")
    git(repo, "replace", third, replacement)
    assert git(repo, "merge-base", "--is-ancestor", sibling, third) == ""
    result = wave.verify_wave(repo, [sibling], third)
    assert not result.ok and not result.unknown
    assert result.results[0].status == "not-ancestor"


def test_git_timeout_fails_closed(history, monkeypatch):
    repo, commits, _ = history

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(wave.subprocess, "run", timeout)
    assert wave.verify_wave(repo, commits, commits[-1]).unknown
    with pytest.raises(wave.GitVerificationError):
        wave.resolve_revisions(repo, commits)


def test_plan_interleaves_groups_without_repeating_services():
    assert wave.plan_wave(["a", "b", "c", "a"], {"a": "shared", "b": "shared"}) == ["a", "c", "b"]


def test_whole_proof_has_one_subprocess_deadline(history, monkeypatch):
    repo, commits, _ = history
    now = [0.0]
    run = wave.subprocess.run
    timeouts = []

    def advance(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += 3
        return run(*args, **kwargs)

    monkeypatch.setattr(wave.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(wave.subprocess, "run", advance)
    result = wave.verify_wave(repo, commits[:2], commits[-1], timeout=5)
    assert result.unknown
    assert timeouts == [5, 2]
