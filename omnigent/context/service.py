"""File operations over a project's context repository.

:class:`ContextService` is pure Python with no database: it is constructed per
request from a validated :class:`~omnigent.context.config.ContextConfig` and
exposes the read/write/search/injection/graph primitives the REST routes and
agent tools share (``designs/PROJECT_CONTEXT.md`` §4.1).

Security model (§6):

- Every client-supplied path goes through :meth:`ContextService.resolve`,
  which rejects absolute paths, ``..`` traversal, NUL bytes, hidden
  components, anything outside the four content folders (plus
  ``CONTEXT.md``), and symlinks whose target escapes the root.
- Writes are limited to ``.md`` / ``.txt`` files under ``system/`` and
  ``wiki/``, capped at :data:`MAX_WRITE_BYTES`, and use optimistic
  concurrency (``base_sha``) so a stale editor cannot clobber a newer save.
- Secret-looking files under ``raw/`` are listed but never read, searched, or
  injected.
- ``git`` runs with argv lists and timeouts (see :mod:`omnigent.context.git`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from omnigent.context import code_graph as code_graph_mod
from omnigent.context.config import ContextConfig
from omnigent.context.git import git_commit_paths, git_log, git_toplevel
from omnigent.errors import ErrorCode, OmnigentError

#: Folders a context repository is made of, in display order.
CONTENT_DIRS = ("system", "wiki", "raw", "graph")
#: Folders whose markdown may be created/edited/deleted from the API.
WRITABLE_DIRS = ("system", "wiki")
#: File suffixes accepted for writes.
WRITABLE_SUFFIXES = (".md", ".txt")
#: Top-level files (outside the content folders) that may be read.
ROOT_FILES = ("CONTEXT.md",)
#: Hidden bookkeeping directory for pending Update-context proposals.
PROPOSALS_DIR = ".proposals"
#: Hidden directory for per-session context traces.
TRACES_DIR = ".traces"
#: Stamp written at the end of every Update-context run.
LAST_UPDATE_FILE = ".last_update"

#: Per-file write cap.
MAX_WRITE_BYTES = 1024 * 1024
#: Largest file returned whole by :meth:`ContextService.read`.
MAX_READ_BYTES = 2 * 1024 * 1024
#: Largest file scanned by search / link extraction.
MAX_SCAN_BYTES = 1024 * 1024
#: Default cap on the injected startup text.
DEFAULT_INJECT_CHARS = 12_000
#: Top-level profile files a harness already loads from the user's own config
#: (lower-cased). Claude Code reads ``~/.claude/CLAUDE.md`` itself, so a profile
#: ``CLAUDE.md`` (typically that file, symlinked) would otherwise arrive twice.
NATIVELY_LOADED_PROFILE_FILES: dict[str, frozenset[str]] = {
    "claude-native": frozenset({"claude.md"}),
}
#: Upper bound on tree entries returned.
MAX_TREE_ENTRIES = 5000

#: The fixed closing note of every injected context block.
TOOL_USAGE_NOTE = (
    "Project context tools are available: context_list, context_read, "
    "context_search, graph_query, graph_neighbors. Check them before exploring "
    "broadly or when the task touches project history, decisions, experiments, "
    "or cross-file structure."
)

_SECRET_NAME_RE = re.compile(
    r"(^\.env)|(\.pem$)|(\.key$)|(\.p12$)|(\.pfx$)|(^id_(rsa|ed25519|ecdsa|dsa))"
    r"|(credential)|(secret)|(\.netrc$)|(\.npmrc$)|(\.pypirc$)",
    re.IGNORECASE,
)
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

_CONTEXT_MD = """\
# Context repository

This folder is a project's context repository, managed by Omnigent.

- `system/` — **always injected** into every session. Keep it SMALL. Only
  non-standard rules and conventions an agent would otherwise get wrong.
  No overviews.
- `wiki/` — fetched on demand through tools. One topic per file. Start each
  file with frontmatter so the index can describe it:

  ```markdown
  ---
  description: One line used in the injected index and search results.
  ---
  ```

- `raw/` — immutable sources (papers, notes, exports). Read-only in the UI.
- `graph/` — derived data (graphify code graph). Do not edit by hand.

Curators: edit incrementally, never delete information without a rationale,
keep `system/` minimal, and cite sources.
"""

_GITIGNORE_LINES = (f"{PROPOSALS_DIR}/", f"{TRACES_DIR}/", LAST_UPDATE_FILE)


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of ``data``.

    :param data: Raw bytes.
    :returns: Lower-case hex digest.
    """
    return hashlib.sha256(data).hexdigest()


def is_secret_name(name: str) -> bool:
    """Whether a file name looks like it holds credentials.

    :param name: A file's base name.
    :returns: ``True`` for ``.env*``, keys, ``*credential*``, ``*secret*``, ...
    """
    return bool(_SECRET_NAME_RE.search(name))


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from a markdown document.

    :param text: The document.
    :returns: ``(frontmatter, body)``; ``({}, text)`` when there is none or it
        does not parse to a mapping.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for idx in range(1, min(len(lines), 200)):
        if lines[idx].strip() in ("---", "..."):
            try:
                meta = yaml.safe_load("".join(lines[1:idx]))
            except yaml.YAMLError:
                return {}, text
            if not isinstance(meta, dict):
                return {}, text
            return meta, "".join(lines[idx + 1 :])
    return {}, text


def _description_of(meta: dict[str, Any]) -> str | None:
    """Pull a single-line description string out of frontmatter.

    :param meta: Parsed frontmatter.
    :returns: The trimmed one-line description, or ``None``.
    """
    value = meta.get("description")
    if not isinstance(value, str):
        return None
    single = " ".join(value.split())
    return single[:300] or None


@dataclasses.dataclass(frozen=True)
class ResolvedPath:
    """A client path that passed confinement checks.

    :param rel: Normalised POSIX path relative to the root, e.g. ``"wiki/a.md"``.
    :param abs_path: The absolute path on disk (not symlink-resolved).
    """

    rel: str
    abs_path: Path

    @property
    def top(self) -> str:
        """First path component (``"wiki"``, ``"CONTEXT.md"``, ...)."""
        return self.rel.split("/", 1)[0]


class ContextService:
    """Operations over one project's context repository.

    :param config: The project's validated context configuration.
    """

    def __init__(self, config: ContextConfig) -> None:
        self.config = config
        self.root = Path(config.path)

    # ── Path confinement ────────────────────────────────────────────

    def _real_root(self) -> Path:
        """Symlink-resolved root used as the confinement boundary.

        :returns: The resolved root path.
        """
        return self.root.resolve(strict=False)

    def resolve(self, rel_path: str) -> ResolvedPath:
        """Validate a client-supplied relative path and map it onto disk.

        :param rel_path: e.g. ``"wiki/experiments.md"``.
        :returns: The normalised, confined path.
        :raises OmnigentError: 400 for traversal, absolute or hidden paths,
            paths outside the content folders, or symlink escapes.
        """
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise OmnigentError("path is required", code=ErrorCode.INVALID_INPUT)
        if "\x00" in rel_path or "\\" in rel_path:
            raise OmnigentError("path contains invalid characters", code=ErrorCode.INVALID_INPUT)
        if rel_path.startswith("/") or re.match(r"^[A-Za-z]:", rel_path):
            raise OmnigentError("path must be relative", code=ErrorCode.INVALID_INPUT)
        parts = [p for p in PurePosixPath(rel_path).parts if p not in ("", ".")]
        if not parts:
            raise OmnigentError("path is required", code=ErrorCode.INVALID_INPUT)
        for part in parts:
            if part == "..":
                raise OmnigentError("path must not contain '..'", code=ErrorCode.INVALID_INPUT)
        # Hidden directories (.git, .proposals, .traces) are never addressable.
        # A hidden *file* is only tolerated under raw/, where it is listed but
        # never readable (secret-like).
        for part in parts[:-1]:
            if part.startswith("."):
                raise OmnigentError(
                    "hidden paths are not accessible", code=ErrorCode.INVALID_INPUT
                )
        if parts[-1].startswith(".") and parts[0] != "raw":
            raise OmnigentError("hidden paths are not accessible", code=ErrorCode.INVALID_INPUT)
        if len(parts) == 1:
            if parts[0] not in ROOT_FILES and parts[0] not in CONTENT_DIRS:
                raise OmnigentError(
                    f"path must be under {', '.join(d + '/' for d in CONTENT_DIRS)}",
                    code=ErrorCode.INVALID_INPUT,
                )
        elif parts[0] not in CONTENT_DIRS:
            raise OmnigentError(
                f"path must be under {', '.join(d + '/' for d in CONTENT_DIRS)}",
                code=ErrorCode.INVALID_INPUT,
            )
        rel = "/".join(parts)
        candidate = self.root.joinpath(*parts)
        real_root = self._real_root()
        # resolve(strict=False) follows every existing symlink along the path,
        # so a link inside the root pointing outside it is caught here even
        # when the final component does not exist yet.
        real = candidate.resolve(strict=False)
        if real != real_root and not real.is_relative_to(real_root):
            raise OmnigentError("path escapes the context directory", code=ErrorCode.INVALID_INPUT)
        return ResolvedPath(rel=rel, abs_path=candidate)

    @staticmethod
    def is_writable_rel(rel: str) -> bool:
        """Whether ``rel`` names an API-writable file.

        :param rel: A path already normalised by :meth:`resolve`.
        :returns: ``True`` for ``.md``/``.txt`` under ``system/`` or ``wiki/``.
        """
        parts = rel.split("/")
        return (
            len(parts) >= 2
            and parts[0] in WRITABLE_DIRS
            and not parts[-1].startswith(".")
            and parts[-1].lower().endswith(WRITABLE_SUFFIXES)
        )

    def _require_writable(self, resolved: ResolvedPath) -> None:
        """Refuse writes outside ``system/`` and ``wiki/`` markdown.

        :param resolved: The target path.
        :raises OmnigentError: 403 for read-only folders, 400 for bad suffixes.
        """
        if resolved.top not in WRITABLE_DIRS:
            raise OmnigentError(
                f"{resolved.top}/ is read-only; only system/ and wiki/ are editable",
                code=ErrorCode.FORBIDDEN,
            )
        if not self.is_writable_rel(resolved.rel):
            raise OmnigentError(
                "only .md and .txt files can be written", code=ErrorCode.INVALID_INPUT
            )

    # ── Status / tree ───────────────────────────────────────────────

    def exists(self) -> bool:
        """Whether the context root is an existing directory.

        :returns: ``True`` when the root exists.
        """
        return self.root.is_dir()

    def initialized(self) -> bool:
        """Whether the scaffold (``system/`` and ``wiki/``) is present.

        :returns: ``True`` once :meth:`init` has run (or equivalent).
        """
        return (self.root / "system").is_dir() and (self.root / "wiki").is_dir()

    def init(self) -> list[str]:
        """Scaffold the directory layout idempotently.

        Creates missing content folders, ``CONTEXT.md``, and a ``.gitignore``
        covering bookkeeping files; existing files are never overwritten.

        :returns: Relative paths that were created.
        :raises OmnigentError: 400 when the root exists but is not a directory,
            or cannot be created.
        """
        if self.root.exists() and not self.root.is_dir():
            raise OmnigentError(
                f"context path {self.root} exists and is not a directory",
                code=ErrorCode.INVALID_INPUT,
            )
        created: list[str] = []
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for name in CONTENT_DIRS:
                target = self.resolve(name).abs_path
                if not target.exists():
                    target.mkdir()
                    created.append(f"{name}/")
            context_md = self.root / "CONTEXT.md"
            if not context_md.exists():
                context_md.write_text(_CONTEXT_MD, encoding="utf-8")
                created.append("CONTEXT.md")
            gitignore = self.root / ".gitignore"
            existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
            missing = [line for line in _GITIGNORE_LINES if line not in existing.splitlines()]
            if missing:
                prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
                gitignore.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")
                if not existing:
                    created.append(".gitignore")
        except OSError as exc:
            raise OmnigentError(
                f"could not initialise context directory: {exc}", code=ErrorCode.INVALID_INPUT
            ) from exc
        if created:
            git_commit_paths(
                self.root,
                [c.rstrip("/") for c in created if (self.root / c.rstrip("/")).is_file()],
                "context: initialise context repository",
            )
        return created

    def iter_files(self) -> list[tuple[str, Path]]:
        """Enumerate content files as ``(rel, abs)`` pairs, sorted.

        Hidden directories are skipped; symlinks whose target escapes the root
        are skipped silently.

        :returns: Sorted file list (at most :data:`MAX_TREE_ENTRIES`).
        """
        real_root = self._real_root()
        out: list[tuple[str, Path]] = []
        context_md = self.root / "CONTEXT.md"
        if context_md.is_file():
            out.append(("CONTEXT.md", context_md))
        for top in CONTENT_DIRS:
            base = self.root / top
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if name.startswith(".") and top != "raw":
                        continue
                    abs_path = Path(dirpath) / name
                    real = abs_path.resolve(strict=False)
                    if not real.is_relative_to(real_root) or not real.is_file():
                        continue
                    rel = abs_path.relative_to(self.root).as_posix()
                    out.append((rel, abs_path))
                    if len(out) >= MAX_TREE_ENTRIES:
                        return out
        return out

    def _read_frontmatter_description(self, abs_path: Path) -> str | None:
        """Read just enough of a markdown file to get its description.

        :param abs_path: A markdown file.
        :returns: The frontmatter description, or ``None``.
        """
        try:
            with abs_path.open("r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(8192)
        except OSError:
            return None
        meta, _ = split_frontmatter(head)
        return _description_of(meta)

    def tree(self) -> dict[str, Any]:
        """List content files with metadata for the Files view.

        :returns: ``{"files": [...], "truncated": bool}``. Each file has
            ``path``, ``size``, ``mtime``, ``description``, ``always_loaded``,
            ``writable``, and ``readable``.
        """
        files: list[dict[str, Any]] = []
        entries = self.iter_files()
        for rel, abs_path in entries:
            try:
                stat = abs_path.stat()
            except OSError:
                continue
            name = rel.rsplit("/", 1)[-1]
            is_md = name.lower().endswith(".md")
            top = rel.split("/", 1)[0]
            files.append(
                {
                    "path": rel,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                    "description": self._read_frontmatter_description(abs_path) if is_md else None,
                    "always_loaded": top == "system" and is_md,
                    "writable": self.is_writable_rel(rel),
                    "readable": not is_secret_name(name),
                }
            )
        return {"files": files, "truncated": len(entries) >= MAX_TREE_ENTRIES}

    # ── Read / write ────────────────────────────────────────────────

    def read(self, rel_path: str) -> dict[str, Any]:
        """Read one file.

        :param rel_path: Relative path.
        :returns: ``{"path", "content", "sha", "size", "writable", "binary",
            "truncated"}``. ``content`` is ``None`` for binary files.
        :raises OmnigentError: 400 bad path, 403 secret-like file, 404 missing.
        """
        resolved = self.resolve(rel_path)
        name = resolved.rel.rsplit("/", 1)[-1]
        if is_secret_name(name):
            raise OmnigentError(
                "this file looks like it contains secrets and cannot be read",
                code=ErrorCode.FORBIDDEN,
            )
        if not resolved.abs_path.is_file():
            raise OmnigentError(f"{resolved.rel} not found", code=ErrorCode.NOT_FOUND)
        try:
            size = resolved.abs_path.stat().st_size
            with resolved.abs_path.open("rb") as handle:
                data = handle.read(MAX_READ_BYTES + 1)
        except OSError as exc:
            raise OmnigentError(
                f"could not read {resolved.rel}: {exc}", code=ErrorCode.NOT_FOUND
            ) from exc
        truncated = len(data) > MAX_READ_BYTES
        data = data[:MAX_READ_BYTES]
        binary = b"\x00" in data[:8192]
        return {
            "path": resolved.rel,
            "content": None if binary else data.decode("utf-8", errors="replace"),
            # The sha covers exactly the bytes a client could save back; a
            # truncated read can never match, so it cannot be edited blindly.
            "sha": sha256_bytes(data) if not truncated else None,
            "size": size,
            "writable": self.is_writable_rel(resolved.rel) and not truncated and not binary,
            "binary": binary,
            "truncated": truncated,
        }

    def current_sha(self, rel_path: str) -> str | None:
        """Sha256 of a file's current bytes, or ``None`` when it is absent.

        :param rel_path: Relative path.
        :returns: Hex digest or ``None``.
        """
        resolved = self.resolve(rel_path)
        if not resolved.abs_path.is_file():
            return None
        return sha256_bytes(resolved.abs_path.read_bytes())

    def _atomic_write(self, resolved: ResolvedPath, content: str) -> None:
        """Write ``content`` to ``resolved`` via a temp file + rename.

        :param resolved: Target path (already confined and write-checked).
        :param content: Text to write.
        :raises OmnigentError: 400 when the content is too large or the write fails.
        """
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise OmnigentError(
                f"file exceeds the {MAX_WRITE_BYTES} byte limit", code=ErrorCode.INVALID_INPUT
            )
        parent = resolved.abs_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            # Re-check after mkdir: a racing symlink swap must not redirect us.
            self.resolve(resolved.rel)
            fd, tmp_name = tempfile.mkstemp(prefix=".ctx-", dir=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp_name, resolved.abs_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            raise OmnigentError(
                f"could not write {resolved.rel}: {exc}", code=ErrorCode.INVALID_INPUT
            ) from exc

    def write(
        self,
        rel_path: str,
        content: str,
        base_sha: str | None,
        *,
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        """Replace (or create) a writable file with optimistic concurrency.

        :param rel_path: Relative path under ``system/`` or ``wiki/``.
        :param content: New file content.
        :param base_sha: Sha of the content the client loaded; ``None`` asserts
            the file does not exist yet.
        :param commit_message: Git commit message; defaults to
            ``"context: update <path>"``.
        :returns: ``{"path", "sha", "size", "commit"}``.
        :raises OmnigentError: 403/400 for disallowed targets, 409 when
            ``base_sha`` no longer matches the file on disk.
        """
        resolved = self.resolve(rel_path)
        self._require_writable(resolved)
        if resolved.abs_path.exists() and not resolved.abs_path.is_file():
            raise OmnigentError(f"{resolved.rel} is not a file", code=ErrorCode.INVALID_INPUT)
        current = self.current_sha(resolved.rel)
        if current != base_sha:
            raise OmnigentError(
                f"{resolved.rel} changed since it was loaded; reload and re-apply your edit",
                code=ErrorCode.CONFLICT,
            )
        self._atomic_write(resolved, content)
        commit = git_commit_paths(
            self.root, [resolved.rel], commit_message or f"context: update {resolved.rel}"
        )
        data = content.encode("utf-8")
        return {
            "path": resolved.rel,
            "sha": sha256_bytes(data),
            "size": len(data),
            "commit": commit,
        }

    def create(self, rel_path: str, content: str = "") -> dict[str, Any]:
        """Create a new writable file.

        :param rel_path: Relative path under ``system/`` or ``wiki/``.
        :param content: Initial content.
        :returns: Same shape as :meth:`write`.
        :raises OmnigentError: 409 when the file already exists.
        """
        resolved = self.resolve(rel_path)
        self._require_writable(resolved)
        if resolved.abs_path.exists():
            raise OmnigentError(f"{resolved.rel} already exists", code=ErrorCode.CONFLICT)
        return self.write(
            resolved.rel, content, None, commit_message=f"context: create {resolved.rel}"
        )

    def delete(self, rel_path: str) -> dict[str, Any]:
        """Delete a writable file.

        :param rel_path: Relative path under ``system/`` or ``wiki/``.
        :returns: ``{"path", "deleted": True, "commit"}``.
        :raises OmnigentError: 404 when missing.
        """
        resolved = self.resolve(rel_path)
        self._require_writable(resolved)
        if not resolved.abs_path.is_file():
            raise OmnigentError(f"{resolved.rel} not found", code=ErrorCode.NOT_FOUND)
        try:
            resolved.abs_path.unlink()
        except OSError as exc:
            raise OmnigentError(
                f"could not delete {resolved.rel}: {exc}", code=ErrorCode.INVALID_INPUT
            ) from exc
        commit = git_commit_paths(self.root, [resolved.rel], f"context: delete {resolved.rel}")
        return {"path": resolved.rel, "deleted": True, "commit": commit}

    def rename(self, rel_path: str, new_rel_path: str) -> dict[str, Any]:
        """Rename a writable file to another writable location.

        :param rel_path: Existing path.
        :param new_rel_path: Destination path (must not exist).
        :returns: ``{"path", "old_path", "commit"}``.
        :raises OmnigentError: 404 missing source, 409 existing destination.
        """
        source = self.resolve(rel_path)
        dest = self.resolve(new_rel_path)
        self._require_writable(source)
        self._require_writable(dest)
        if not source.abs_path.is_file():
            raise OmnigentError(f"{source.rel} not found", code=ErrorCode.NOT_FOUND)
        if dest.abs_path.exists():
            raise OmnigentError(f"{dest.rel} already exists", code=ErrorCode.CONFLICT)
        try:
            dest.abs_path.parent.mkdir(parents=True, exist_ok=True)
            self.resolve(dest.rel)
            os.replace(source.abs_path, dest.abs_path)
        except OSError as exc:
            raise OmnigentError(
                f"could not rename {source.rel}: {exc}", code=ErrorCode.INVALID_INPUT
            ) from exc
        commit = git_commit_paths(
            self.root, [source.rel, dest.rel], f"context: rename {source.rel} -> {dest.rel}"
        )
        return {"path": dest.rel, "old_path": source.rel, "commit": commit}

    # ── Search ──────────────────────────────────────────────────────

    def _scannable_files(self) -> list[tuple[str, Path]]:
        """Text files eligible for search and linking.

        :returns: ``(rel, abs)`` for ``.md``/``.txt`` files that are not
            secret-like and not oversized, excluding ``graph/``'s JSON.
        """
        out: list[tuple[str, Path]] = []
        for rel, abs_path in self.iter_files():
            name = rel.rsplit("/", 1)[-1]
            if is_secret_name(name) or not name.lower().endswith(WRITABLE_SUFFIXES):
                continue
            try:
                if abs_path.stat().st_size > MAX_SCAN_BYTES:
                    continue
            except OSError:
                continue
            out.append((rel, abs_path))
        return out

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Case-insensitive substring/token search over markdown and text files.

        A line containing the whole query scores highest; otherwise lines are
        scored by how many query tokens they contain.

        :param query: Search text.
        :param limit: Maximum hits (clamped to ``1..200``).
        :returns: ``{"query", "results": [{"path", "line", "snippet", "score"}],
            "truncated"}``.
        """
        limit = max(1, min(limit, 200))
        needle = " ".join(query.lower().split())
        if not needle:
            raise OmnigentError("query is required", code=ErrorCode.INVALID_INPUT)
        tokens = sorted(set(needle.split()))
        hits: list[dict[str, Any]] = []
        for rel, abs_path in self._scannable_files():
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            path_bonus = 1 if any(tok in rel.lower() for tok in tokens) else 0
            for lineno, line in enumerate(text.splitlines(), start=1):
                lowered = line.lower()
                if needle in lowered:
                    score = len(tokens) + 2
                else:
                    score = sum(1 for tok in tokens if tok in lowered)
                    # Multi-word queries need at least half the words on a line.
                    if score * 2 < len(tokens) or score == 0:
                        continue
                stripped = line.strip()
                snippet = stripped if len(stripped) <= 240 else stripped[:237] + "..."
                hits.append(
                    {"path": rel, "line": lineno, "snippet": snippet, "score": score + path_bonus}
                )
        hits.sort(key=lambda h: (-h["score"], h["path"], h["line"]))
        return {"query": query, "results": hits[:limit], "truncated": len(hits) > limit}

    # ── Injection ───────────────────────────────────────────────────

    def _profile_files(self) -> list[tuple[str, Path]]:
        """Markdown files from the optional profile directory, sorted.

        :returns: ``(display_name, abs)`` pairs confined to the profile root.
        """
        if not self.config.profile_path:
            return []
        profile_root = Path(self.config.profile_path)
        if not profile_root.is_dir():
            return []
        real_root = profile_root.resolve()
        out: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(profile_root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or not name.lower().endswith(".md"):
                    continue
                abs_path = Path(dirpath) / name
                if not abs_path.resolve().is_relative_to(real_root):
                    continue
                out.append((abs_path.relative_to(profile_root).as_posix(), abs_path))
        out.sort(key=lambda pair: pair[0])
        return out

    def _body_of(self, abs_path: Path) -> str:
        """Read a markdown file's body without frontmatter.

        :param abs_path: File to read.
        :returns: The trimmed body, or ``""`` when unreadable/oversized.
        """
        try:
            if abs_path.stat().st_size > MAX_SCAN_BYTES:
                return ""
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        _, body = split_frontmatter(text)
        return body.strip()

    def natively_loaded_profile_files(self, harness: str | None) -> list[str]:
        """Profile files ``harness`` loads on its own, so injection skips them.

        :param harness: Harness being launched, e.g. ``"claude-native"``; ``None``
            or an unknown harness skips nothing.
        :returns: ``profile/<name>`` entries, sorted.
        """
        names = NATIVELY_LOADED_PROFILE_FILES.get(harness or "", frozenset())
        return [f"profile/{name}" for name, _ in self._profile_files() if name.lower() in names]

    def injected_context(
        self, max_chars: int = DEFAULT_INJECT_CHARS, harness: str | None = None
    ) -> dict[str, Any]:
        """Build the deterministic startup text a session receives.

        Order: profile files, ``system/*.md`` (sorted), a wiki index
        (``path — description``), then :data:`TOOL_USAGE_NOTE`. No timestamps
        or other volatile data, so the prefix stays KV-cache friendly. When
        the text exceeds ``max_chars`` the body is cut at a line boundary with
        an explicit truncation notice; the tool note is always kept.

        :param max_chars: Character cap for the whole text.
        :param harness: Harness being launched; profile files it already loads
            itself (:data:`NATIVELY_LOADED_PROFILE_FILES`) are left out.
        :returns: ``{"text", "chars", "approx_tokens", "truncated", "sha256",
            "files", "skipped"}`` where ``files`` lists the sources included and
            ``skipped`` the profile files left out for ``harness``.
        """
        sections: list[str] = ["# Project context"]
        files: list[str] = []
        skipped = self.natively_loaded_profile_files(harness)
        profile = [(n, p) for n, p in self._profile_files() if f"profile/{n}" not in skipped]
        if profile:
            sections.append("## About the user")
            for name, abs_path in profile:
                body = self._body_of(abs_path)
                if body:
                    sections.append(f"### profile/{name}\n\n{body}")
                    files.append(f"profile/{name}")
        system_files = [
            (rel, abs_path)
            for rel, abs_path in self.iter_files()
            if rel.startswith("system/") and rel.lower().endswith(".md")
        ]
        if system_files:
            sections.append("## Project rules (system/)")
            for rel, abs_path in system_files:
                body = self._body_of(abs_path)
                if body:
                    sections.append(f"### {rel}\n\n{body}")
                    files.append(rel)
        index_lines = []
        for rel, abs_path in self.iter_files():
            if rel.startswith("wiki/") and rel.lower().endswith(".md"):
                desc = self._read_frontmatter_description(abs_path)
                index_lines.append(f"- {rel}" + (f" — {desc}" if desc else ""))
        if index_lines:
            sections.append("## Wiki index (read with context_read)\n\n" + "\n".join(index_lines))
        body = "\n\n".join(sections)
        note = f"\n\n## Context tools\n\n{TOOL_USAGE_NOTE}"
        truncated = False
        budget = max(0, max_chars - len(note))
        if len(body) > budget:
            omitted_marker = (
                "\n\n[... project context truncated: {n} characters omitted; "
                "use context_list / context_read for the rest]"
            )
            keep = max(0, budget - len(omitted_marker.format(n=len(body))))
            cut = body.rfind("\n", 0, keep)
            cut = cut if cut > 0 else keep
            body = body[:cut] + omitted_marker.format(n=len(body) - cut)
            truncated = True
        text = body + note
        return {
            "text": text,
            "chars": len(text),
            "approx_tokens": (len(text) + 3) // 4,
            "truncated": truncated,
            "sha256": sha256_bytes(text.encode("utf-8")),
            "files": files,
            "skipped": skipped,
        }

    def index(self) -> dict[str, Any]:
        """The ``context_list`` payload: system and wiki files with descriptions.

        :returns: ``{"files": [{"path", "description", "always_loaded", "size"}],
            "has_code_graph"}``.
        """
        files = [
            {
                "path": f["path"],
                "description": f["description"],
                "always_loaded": f["always_loaded"],
                "size": f["size"],
            }
            for f in self.tree()["files"]
            if f["path"].startswith(("system/", "wiki/")) or f["path"] == "CONTEXT.md"
        ]
        return {"files": files, "has_code_graph": self.code_graph_path().is_file()}

    # ── Graphs ──────────────────────────────────────────────────────

    def knowledge_graph(self) -> dict[str, Any]:
        """Link graph over markdown files (``[[wikilinks]]`` and relative links).

        :returns: ``{"nodes": [{"id", "label", "group", "description"}],
            "edges": [{"source", "target", "relation"}], "truncated"}``.
        """
        files = [(rel, p) for rel, p in self._scannable_files() if rel.lower().endswith(".md")]
        by_rel = dict(files)
        by_stem: dict[str, list[str]] = {}
        for rel in by_rel:
            stem = PurePosixPath(rel).stem.lower()
            by_stem.setdefault(stem, []).append(rel)
            by_stem.setdefault(rel[:-3].lower(), []).append(rel)
        nodes: list[dict[str, Any]] = []
        edges: set[tuple[str, str, str]] = set()
        for rel, abs_path in files:
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = split_frontmatter(text)
            title = meta.get("title") if isinstance(meta.get("title"), str) else None
            nodes.append(
                {
                    "id": rel,
                    "label": title or PurePosixPath(rel).stem,
                    "group": rel.split("/", 1)[0] if "/" in rel else "root",
                    "description": _description_of(meta),
                }
            )
            for match in _WIKILINK_RE.finditer(body):
                target = match.group(1).strip().lower().removesuffix(".md")
                candidates = by_stem.get(target) or by_stem.get(target.rsplit("/", 1)[-1])
                if candidates:
                    dest = sorted(candidates)[0]
                    if dest != rel:
                        edges.add((rel, dest, "wikilink"))
            for match in _MD_LINK_RE.finditer(body):
                href = match.group(1).split("#", 1)[0]
                if not href or "://" in href or href.startswith(("/", "mailto:")):
                    continue
                joined = os.path.normpath(os.path.join(os.path.dirname(rel), href))
                joined = PurePosixPath(joined).as_posix()
                if joined in by_rel and joined != rel:
                    edges.add((rel, joined, "link"))
        return {
            "nodes": nodes,
            "edges": [{"source": s, "target": t, "relation": r} for s, t, r in sorted(edges)],
            "truncated": False,
        }

    def code_graph_path(self) -> Path:
        """Location of the graphify graph for this project.

        :returns: ``<root>/graph/graph.json``.
        """
        return self.root / "graph" / "graph.json"

    def _load_code_graph(self) -> code_graph_mod.CodeGraph | None:
        """Load the confined code graph, if present.

        :returns: The parsed graph or ``None``.
        """
        resolved = self.resolve("graph/graph.json")
        return code_graph_mod.load_code_graph(resolved.abs_path)

    def code_graph(self, limit: int = 1500) -> dict[str, Any]:
        """The code graph capped for the UI.

        :param limit: Maximum nodes (highest degree first).
        :returns: See :func:`omnigent.context.code_graph.ui_graph`, plus
            ``available``.
        """
        graph = self._load_code_graph()
        if graph is None:
            return {
                "available": False,
                "nodes": [],
                "edges": [],
                "total_nodes": 0,
                "total_edges": 0,
                "truncated": False,
            }
        return {"available": True, **code_graph_mod.ui_graph(graph, limit)}

    def graph_query(self, question: str, budget: int = 2000) -> dict[str, Any]:
        """Relevant code-graph nodes/edges for a question.

        :param question: Natural-language question.
        :param budget: Approximate token budget for the text.
        :returns: ``{"available", "text", "seeds", "node_ids", "truncated"}``.
        """
        if not question.strip():
            raise OmnigentError("question is required", code=ErrorCode.INVALID_INPUT)
        graph = self._load_code_graph()
        if graph is None:
            return {
                "available": False,
                "text": "This project has no code graph yet (run Update context).",
                "seeds": [],
                "node_ids": [],
                "truncated": False,
            }
        return {"available": True, **code_graph_mod.query(graph, question, budget)}

    def graph_neighbors(self, node: str, depth: int = 1) -> dict[str, Any]:
        """Neighbours of a code-graph node.

        :param node: Node id or label.
        :param depth: 1 or 2.
        :returns: See :func:`omnigent.context.code_graph.neighbors`, plus
            ``available``.
        """
        if not node.strip():
            raise OmnigentError("node is required", code=ErrorCode.INVALID_INPUT)
        graph = self._load_code_graph()
        if graph is None:
            return {
                "available": False,
                "node": None,
                "neighbors": [],
                "truncated": False,
                "candidates": [],
            }
        return {"available": True, **code_graph_mod.neighbors(graph, node, depth)}

    # ── History / status ────────────────────────────────────────────

    def history(self, limit: int = 50) -> dict[str, Any]:
        """Git history of the context directory.

        :param limit: Maximum commits (clamped to ``1..500``).
        :returns: ``{"versioned", "commits": [...]}``.
        """
        if git_toplevel(self.root) is None:
            return {"versioned": False, "commits": []}
        commits = git_log(self.root, max(1, min(limit, 500)))
        return {"versioned": True, "commits": [dataclasses.asdict(c) for c in commits]}

    def last_update(self) -> float | None:
        """Unix time at which the last Update-context run gathered its inputs.

        :returns: Timestamp, or ``None`` when never run.
        """
        stamp = self.root / LAST_UPDATE_FILE
        try:
            payload = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = payload.get("timestamp") if isinstance(payload, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    def write_last_update(self, timestamp: float | None = None) -> None:
        """Record the completion time of an Update-context run.

        :param timestamp: Unix time; defaults to now.
        """
        stamp = self.root / LAST_UPDATE_FILE
        stamp.write_text(
            json.dumps({"timestamp": timestamp if timestamp is not None else time.time()}),
            encoding="utf-8",
        )

    def status(self) -> dict[str, Any]:
        """Summary for the Context page header and empty state.

        :returns: Paths, existence, git/graphify availability, tree, and the
            injected size.
        """
        exists = self.exists()
        payload: dict[str, Any] = {
            "configured": True,
            "config": self.config.to_dict(),
            "exists": exists,
            "initialized": exists and self.initialized(),
            "git": None,
            "graphify_available": shutil.which("graphify") is not None,
            "has_code_graph": False,
            "tree": {"files": [], "truncated": False},
            "injected": None,
            "last_update": None,
        }
        if not exists:
            return payload
        top = git_toplevel(self.root)
        payload["git"] = {"toplevel": top} if top else None
        payload["has_code_graph"] = self.code_graph_path().is_file()
        payload["tree"] = self.tree()
        injected = self.injected_context()
        payload["injected"] = {
            "chars": injected["chars"],
            "approx_tokens": injected["approx_tokens"],
            "truncated": injected["truncated"],
        }
        payload["last_update"] = self.last_update()
        return payload
