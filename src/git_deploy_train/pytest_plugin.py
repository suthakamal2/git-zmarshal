"""Backwards-compatible re-export of ``git_zmarshal.pytest_plugin``."""

from git_zmarshal.pytest_plugin import *  # noqa: F401,F403
from git_zmarshal.pytest_plugin import _cli, _exempt  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(_cli())
