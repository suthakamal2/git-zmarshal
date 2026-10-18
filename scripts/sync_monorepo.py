#!/usr/bin/env python3
"""Two-way sync & isolated-runtime verifier between ``git-zmarshal`` and Cortex ``packages/git-deploy-train``.

Why this exists:
1. ``src/git_zmarshal/`` (canonical public package) and ``src/git_deploy_train/``
   (Cortex backwards-compatible package) are maintained as byte-for-byte mirrors.
   Because every module uses only Python stdlib + relative/sibling imports, both
   packages work as normal ``pip`` installs AND when copied into an isolated root
   daemon directory loaded under ``python3 -I -S`` via ``spec_from_file_location``.
2. Changes made in the standalone ``suthakamal2/git-zmarshal`` repository can be
   pushed into the Cortex monorepo (``--push-to-monorepo``), and changes made inside
   ``packages/git-deploy-train`` in Cortex can be pulled back into the standalone
   repository (``--pull-from-monorepo``).
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "__init__.py",
    "cli.py",
    "guard.py",
    "py.typed",
    "pytest_plugin.py",
    "train.py",
    "wave.py",
    "xcode.py",
)
SYNCED_PATHS = (
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "examples",
    "scripts",
    "src",
    "tests",
)


def sync_shim(root: Path = REPO_ROOT, *, source_pkg: str = "git_zmarshal") -> list[str]:
    """Mirror ``src/<source_pkg>/*`` into the sibling package directory."""
    target_pkg = "git_deploy_train" if source_pkg == "git_zmarshal" else "git_zmarshal"
    src_dir = root / "src" / source_pkg
    dst_dir = root / "src" / target_pkg
    dst_dir.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []
    for name in MODULES:
        s_file = src_dir / name
        d_file = dst_dir / name
        if not s_file.exists():
            raise FileNotFoundError(f"Missing module {s_file}")
        if not d_file.exists() or s_file.read_bytes() != d_file.read_bytes():
            shutil.copy2(s_file, d_file)
            updated.append(name)
    return updated


def check_shim_parity(root: Path = REPO_ROOT) -> list[str]:
    """Return a list of drift descriptions if ``src/git_zmarshal`` and ``src/git_deploy_train`` differ."""
    zm_dir = root / "src" / "git_zmarshal"
    dt_dir = root / "src" / "git_deploy_train"
    drifts: list[str] = []
    for name in MODULES:
        zf = zm_dir / name
        df = dt_dir / name
        if not zf.exists():
            drifts.append(f"missing src/git_zmarshal/{name}")
        elif not df.exists():
            drifts.append(f"missing src/git_deploy_train/{name}")
        elif zf.read_bytes() != df.read_bytes():
            drifts.append(f"content mismatch: src/git_zmarshal/{name} != src/git_deploy_train/{name}")
    return drifts


def check_isolated_imports(root: Path = REPO_ROOT) -> None:
    """Verify every module loads under ``python -I -S`` via ``spec_from_file_location``."""
    code = """
import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
for pkg in ("git_zmarshal", "git_deploy_train"):
    pkg_dir = root / "src" / pkg
    for mod_name in ("guard", "train", "wave", "xcode", "pytest_plugin"):
        path = pkg_dir / f"{mod_name}.py"
        spec = importlib.util.spec_from_file_location(f"_isolated_{pkg}_{mod_name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
"""
    subprocess.run([sys.executable, "-I", "-S", "-c", code, str(root)], check=True)


def _copy_tree_clean(src: Path, dst: Path) -> None:
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.egg-info"),
        )
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def compare_roots(standalone_root: Path, monorepo_pkg_root: Path) -> list[str]:
    """Compare synced files between standalone ``git-zmarshal`` and ``<monorepo>/packages/git-deploy-train``."""
    diffs: list[str] = []
    for rel in SYNCED_PATHS:
        s_path = standalone_root / rel
        m_path = monorepo_pkg_root / rel
        if not s_path.exists() or not m_path.exists():
            diffs.append(f"missing {rel} in {'standalone' if not s_path.exists() else 'monorepo'}")
            continue
        if s_path.is_file():
            if s_path.read_bytes() != m_path.read_bytes():
                diffs.append(rel)
        else:
            for f in sorted(s_path.rglob("*")):
                if f.is_dir() or "__pycache__" in f.parts or f.suffix == ".pyc":
                    continue
                sub = f.relative_to(s_path)
                mf = m_path / sub
                if not mf.exists() or not filecmp.cmp(f, mf, shallow=False):
                    diffs.append(str(Path(rel) / sub))
    return diffs


def push_to_monorepo(standalone_root: Path, monorepo_pkg_root: Path) -> list[str]:
    sync_shim(standalone_root, source_pkg="git_zmarshal")
    check_isolated_imports(standalone_root)
    monorepo_pkg_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for rel in SYNCED_PATHS:
        _copy_tree_clean(standalone_root / rel, monorepo_pkg_root / rel)
        copied.append(rel)
    return copied


def pull_from_monorepo(standalone_root: Path, monorepo_pkg_root: Path) -> list[str]:
    if not monorepo_pkg_root.exists():
        raise FileNotFoundError(f"Monorepo package path not found: {monorepo_pkg_root}")
    # If a developer edited src/git_deploy_train in the monorepo without touching src/git_zmarshal,
    # detect which tree has newer changes before mirroring.
    zm_dir = monorepo_pkg_root / "src" / "git_zmarshal"
    dt_dir = monorepo_pkg_root / "src" / "git_deploy_train"
    source_pkg = "git_zmarshal"
    if dt_dir.exists() and zm_dir.exists():
        dt_mtime = max((p.stat().st_mtime for p in dt_dir.glob("*.py")), default=0.0)
        zm_mtime = max((p.stat().st_mtime for p in zm_dir.glob("*.py")), default=0.0)
        if dt_mtime > zm_mtime + 0.001:
            source_pkg = "git_deploy_train"
    sync_shim(monorepo_pkg_root, source_pkg=source_pkg)
    copied: list[str] = []
    for rel in SYNCED_PATHS:
        if (monorepo_pkg_root / rel).exists():
            _copy_tree_clean(monorepo_pkg_root / rel, standalone_root / rel)
            copied.append(rel)
    sync_shim(standalone_root, source_pkg="git_zmarshal")
    check_isolated_imports(standalone_root)
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--monorepo",
        type=Path,
        default=None,
        help="Path to the Cortex monorepo root (contains packages/git-deploy-train)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync-shim", action="store_true", help="Mirror src/git_zmarshal into src/git_deploy_train")
    group.add_argument("--check", action="store_true", help="Verify shim parity, python -I -S loading, and optional monorepo parity")
    group.add_argument("--push-to-monorepo", action="store_true", help="Sync standalone repo into <monorepo>/packages/git-deploy-train")
    group.add_argument("--pull-from-monorepo", action="store_true", help="Sync <monorepo>/packages/git-deploy-train into standalone repo")
    args = parser.parse_args(argv)

    pkg_subpath = Path("packages") / "git-deploy-train"
    monorepo_pkg = (
        (args.monorepo / pkg_subpath if (args.monorepo / pkg_subpath).exists() or not (args.monorepo / "pyproject.toml").exists() else args.monorepo)
        if args.monorepo
        else None
    )

    if args.sync_shim:
        updated = sync_shim(REPO_ROOT)
        check_isolated_imports(REPO_ROOT)
        print(f"Synced shim ({len(updated)} file(s) updated): {', '.join(updated) or 'already identical'}")
        return 0

    if args.check:
        drifts = check_shim_parity(REPO_ROOT)
        if drifts:
            for d in drifts:
                print(f"DRIFT: {d}", file=sys.stderr)
            return 1
        check_isolated_imports(REPO_ROOT)
        if monorepo_pkg is not None:
            m_diffs = compare_roots(REPO_ROOT, monorepo_pkg)
            if m_diffs:
                for d in m_diffs:
                    print(f"MONOREPO DRIFT: {d}", file=sys.stderr)
                return 1
        print("OK: git_zmarshal == git_deploy_train (isolated python -I -S verified)")
        return 0

    if monorepo_pkg is None:
        parser.error("--monorepo is required with --push-to-monorepo or --pull-from-monorepo")

    if args.push_to_monorepo:
        copied = push_to_monorepo(REPO_ROOT, monorepo_pkg)
        print(f"Pushed {', '.join(copied)} -> {monorepo_pkg}")
        return 0

    if args.pull_from_monorepo:
        copied = pull_from_monorepo(REPO_ROOT, monorepo_pkg)
        print(f"Pulled {', '.join(copied)} <- {monorepo_pkg}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
