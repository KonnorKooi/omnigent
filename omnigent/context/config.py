"""Parsing and validation of a project's ``context`` config block.

A project's opaque ``config`` JSON may carry a ``context`` object pointing at
a context repository on the server's machine (see
``designs/PROJECT_CONTEXT.md`` §3)::

    {"context": {"path": "~/context/projects/x", "profile_path": "...",
                 "repo_path": "..."}}

The projects API stores the object whole; this module is the one place that
decides whether it is usable. Paths are ``~``-expanded, must be absolute, and
may not contain NUL bytes or name the filesystem root.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

#: Key under ``project.config`` holding the context settings.
CONTEXT_CONFIG_KEY = "context"

_MAX_PATH_CHARS = 4096


@dataclasses.dataclass(frozen=True)
class ContextConfig:
    """A validated project context configuration.

    :param path: Absolute, normalised context repository directory.
    :param profile_path: Optional absolute shared "about me" directory whose
        markdown files are injected before the project's own ``system/``.
    :param repo_path: Optional absolute source repository; enables the code
        graph refresh in the Update context job.
    """

    path: str
    profile_path: str | None = None
    repo_path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serialise for API responses.

        :returns: ``{"path", "profile_path", "repo_path"}``.
        """
        return dataclasses.asdict(self)


def _normalise_path(key: str, value: Any, *, required: bool) -> str | None:
    """Validate and normalise one configured path.

    :param key: The config key being validated, used in error messages.
    :param value: The raw JSON value.
    :param required: Whether a missing/empty value is an error.
    :returns: The absolute normalised path, or ``None`` when optional and unset.
    :raises OmnigentError: 400 when the value is not a usable absolute path.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise OmnigentError(f"context.{key} is required", code=ErrorCode.INVALID_INPUT)
        return None
    if not isinstance(value, str):
        raise OmnigentError(f"context.{key} must be a string", code=ErrorCode.INVALID_INPUT)
    if "\x00" in value:
        raise OmnigentError(
            f"context.{key} must not contain NUL bytes", code=ErrorCode.INVALID_INPUT
        )
    if len(value) > _MAX_PATH_CHARS:
        raise OmnigentError(f"context.{key} is too long", code=ErrorCode.INVALID_INPUT)
    expanded = os.path.expanduser(value.strip())
    if not os.path.isabs(expanded):
        raise OmnigentError(
            f"context.{key} must be an absolute path (or start with ~)",
            code=ErrorCode.INVALID_INPUT,
        )
    normalised = os.path.normpath(expanded)
    # A context root at "/" would scaffold directories into the filesystem
    # root; nothing legitimate needs that.
    if normalised == os.path.sep:
        raise OmnigentError(
            f"context.{key} must not be the filesystem root", code=ErrorCode.INVALID_INPUT
        )
    return normalised


def parse_context_config(project_config: dict[str, Any] | None) -> ContextConfig | None:
    """Extract and validate the ``context`` block from a project config.

    :param project_config: The project's stored ``config`` object.
    :returns: A :class:`ContextConfig`, or ``None`` when the project has no
        ``context`` key (or it is an empty object / ``null``).
    :raises OmnigentError: 400 when the block is present but malformed.
    """
    if not project_config:
        return None
    raw = project_config.get(CONTEXT_CONFIG_KEY)
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError("context must be an object", code=ErrorCode.INVALID_INPUT)
    path = _normalise_path("path", raw.get("path"), required=True)
    assert path is not None
    return ContextConfig(
        path=path,
        profile_path=_normalise_path("profile_path", raw.get("profile_path"), required=False),
        repo_path=_normalise_path("repo_path", raw.get("repo_path"), required=False),
    )


def validate_project_config_context(project_config: dict[str, Any] | None) -> None:
    """Reject a project config whose ``context`` block is malformed.

    Called by the projects API on create/update so a bad path is reported when
    it is saved rather than on first use. The stored value is left verbatim
    (``~`` is expanded at use time).

    :param project_config: The config object being saved.
    :raises OmnigentError: 400 when ``context`` is present but invalid.
    """
    parse_context_config(project_config)
