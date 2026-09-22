"""Minimal git plumbing for a project context repository.

Every call runs ``git`` with an argv list (never a shell), a hard timeout, and
prompts disabled, so a misconfigured credential helper or signing agent can
never hang a request. Failures are reported as return values rather than
exceptions: versioning is a convenience layered on plain files, and a write
must still succeed when the directory is not a repository or git is absent.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)

#: Upper bound for any single git invocation.
GIT_TIMEOUT_S = 20.0

# Field separator for ``git log --format`` parsing (ASCII unit separator).
_FS = "\x1f"
# Record separator between commits (ASCII record separator).
_RS = "\x1e"


@dataclasses.dataclass(frozen=True)
class GitCommit:
    """One entry of a context repository's history.

    :param sha: Full commit hash.
    :param subject: First line of the commit message.
    :param timestamp: Committer time, unix seconds.
    :param author: Author name.
    :param files: Paths touched, relative to the context root.
    """

    sha: str
    subject: str
    timestamp: int
    author: str
    files: list[str]


def _git_env() -> dict[str, str]:
    """Environment for git subprocesses with every interactive prompt disabled.

    :returns: A copy of the process environment with prompt suppression set.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    return env


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``git -C <root> <args>`` with a timeout.

    :param root: Working directory for the command.
    :param args: Git arguments, e.g. ``("log", "-n", "5")``.
    :returns: The completed process, or ``None`` when git is missing, timed
        out, or could not be started.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # argv list, never a shell.
        return subprocess.run(
            [git, "-C", str(root), "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.warning("git %s failed in %s: %s", args[:1], root, exc)
        return None


def git_toplevel(root: Path) -> str | None:
    """Return the repository top-level containing ``root``, if any.

    :param root: A directory that may be inside a git work tree.
    :returns: Absolute top-level path, or ``None`` when not versioned.
    """
    if not root.is_dir():
        return None
    proc = run_git(root, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return top or None


def git_commit_paths(root: Path, rel_paths: list[str], message: str) -> str | None:
    """Stage and commit exactly ``rel_paths`` (additions, edits, deletions).

    Other staged or dirty files in the repository are left alone: the commit
    is scoped with a pathspec so a user's unrelated work is never swept in.

    :param root: The context root (inside a work tree).
    :param rel_paths: Paths relative to ``root`` to commit.
    :param message: Commit message.
    :returns: The new commit sha, or ``None`` when not versioned, nothing
        changed, or git failed (logged).
    """
    if not rel_paths or git_toplevel(root) is None:
        return None
    add = run_git(root, "add", "-A", "--", *rel_paths)
    if add is None or add.returncode != 0:
        _logger.warning(
            "context git add failed in %s: %s", root, add.stderr if add else "unavailable"
        )
        return None
    commit = run_git(root, "commit", "--no-verify", "-m", message, "--", *rel_paths)
    if commit is None or commit.returncode != 0:
        # "nothing to commit" is the common benign case (content unchanged).
        detail = (commit.stdout + commit.stderr) if commit else "unavailable"
        if "nothing to commit" not in detail and "no changes added" not in detail:
            _logger.warning("context git commit failed in %s: %s", root, detail.strip())
        return None
    head = run_git(root, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return None
    return head.stdout.strip() or None


def git_log(root: Path, limit: int) -> list[GitCommit]:
    """Return the most recent commits touching ``root``.

    :param root: The context root.
    :param limit: Maximum number of commits.
    :returns: Newest-first commits; empty when not versioned.
    """
    if git_toplevel(root) is None:
        return []
    proc = run_git(
        root,
        "log",
        f"-n{max(1, limit)}",
        f"--format={_RS}%H{_FS}%s{_FS}%ct{_FS}%an",
        "--name-only",
        "--relative",
        "--",
        ".",
    )
    if proc is None or proc.returncode != 0:
        return []
    commits: list[GitCommit] = []
    for record in proc.stdout.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        header, _, body = record.partition("\n")
        parts = header.split(_FS)
        if len(parts) != 4:
            continue
        sha, subject, ts, author = parts
        try:
            timestamp = int(ts)
        except ValueError:
            timestamp = 0
        files = [line for line in body.splitlines() if line.strip()]
        commits.append(
            GitCommit(sha=sha, subject=subject, timestamp=timestamp, author=author, files=files)
        )
    return commits
