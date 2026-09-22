"""Schema-only tools for reading the session's project context repository.

Registered for every session and relayed to every native harness through the
Omnigent MCP relay. The runner executes them by calling the session-scoped
``/v1/sessions/{id}/context/...`` routes (see
``omnigent/runner/tool_dispatch.py``), so access is always bounded by the
server's owner-only rule and the tools are read-only by construction
(``designs/PROJECT_CONTEXT.md`` §4.3).
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


def _function_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Wrap a parameters object in the OpenAI function-tool envelope.

    :param name: Tool name.
    :param description: LLM-facing description.
    :param parameters: JSON schema for the arguments.
    :returns: The OpenAI-format tool schema.
    """
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


class ContextListTool(Tool):
    """List the project's context files (system rules and wiki topics)."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "context_list"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "List the current project's context repository: always-loaded system/ "
            "rules and on-demand wiki/ topics, each with a one-line description. "
            "Use it to find which wiki page to read before exploring the codebase "
            "broadly. Returns a notice when the session has no project context."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {"type": "object", "properties": {}, "additionalProperties": False},
        )


class ContextReadTool(Tool):
    """Read one file from the project's context repository."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "context_read"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Read a file from the current project's context repository, e.g. "
            "'wiki/experiments.md'. Paths come from context_list or context_search. "
            "Long files are paginated by line: pass offset/limit to continue."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path, e.g. 'wiki/decisions.md'.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based first line to return (default 0).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "description": "Maximum lines to return (default 400).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )


class ContextSearchTool(Tool):
    """Search the project's context repository."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "context_search"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Search the current project's context repository (rules, wiki, raw notes) "
            "for text. Case-insensitive; returns file path, line number and snippet. "
            "Use it when the task touches project history, decisions or experiments."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": "Text to find."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum results (default 10).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )


class GraphQueryTool(Tool):
    """Query the project's code graph."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "graph_query"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Ask the current project's code graph (functions, classes, files and "
            "their calls/uses relationships) a question, e.g. 'where is PSNR "
            "computed'. Returns matching nodes with source locations and nearby "
            "relationships. A navigation aid: confirm by reading the code."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1},
                    "budget": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 8000,
                        "description": "Approximate output token budget (default 2000).",
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        )


class GraphNeighborsTool(Tool):
    """List neighbours of a code-graph node."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "graph_neighbors"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "List what a code-graph node (function, class or file; by id or label) "
            "connects to in the current project, with relation and source location. "
            "Use it to find hidden dependencies before changing shared code."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "node": {"type": "string", "minLength": 1},
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2,
                        "description": "Hops to expand (default 1).",
                    },
                },
                "required": ["node"],
                "additionalProperties": False,
            },
        )


#: Every project-context tool class, in advertised order.
PROJECT_CONTEXT_TOOL_CLASSES: tuple[type[Tool], ...] = (
    ContextListTool,
    ContextReadTool,
    ContextSearchTool,
    GraphQueryTool,
    GraphNeighborsTool,
)

#: Names of the project-context tools.
PROJECT_CONTEXT_TOOL_NAMES: tuple[str, ...] = tuple(
    cls.name() for cls in PROJECT_CONTEXT_TOOL_CLASSES
)
