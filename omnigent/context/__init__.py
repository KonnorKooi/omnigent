"""Project context repositories (``designs/PROJECT_CONTEXT.md``).

A project may point at a directory of human-readable markdown that seeds and
informs every session started in the project, regardless of harness.
"""

from omnigent.context.config import (
    CONTEXT_CONFIG_KEY,
    ContextConfig,
    parse_context_config,
    validate_project_config_context,
)
from omnigent.context.service import ContextService

__all__ = [
    "CONTEXT_CONFIG_KEY",
    "ContextConfig",
    "ContextService",
    "parse_context_config",
    "validate_project_config_context",
]
