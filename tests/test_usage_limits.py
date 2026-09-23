"""Tests for subscription usage-limit parsing, caching, and formatting."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.usage import create_usage_router
from omnigent.usage_limits import (
    USAGE_LIMITS_CACHE,
    UsageLimitsCache,
    format_limits_line,
    provider_report,
    window_from_claude_sdk_info,
    windows_from_antigravity_catalog,
    windows_from_claude_status_line,
    windows_from_codex_rate_limits,
)

NOW = 1_800_000_000


def test_claude_status_line_windows() -> None:
    windows = windows_from_claude_status_line(
        {
            "five_hour": {"used_percentage": 42.25, "resets_at": NOW + 3600},
            "seven_day": {"used_percentage": 18, "resets_at": NOW + 86400 * 3},
            "spend_limit": {"used_percentage": 5},
        }
    )
    assert windows == [
        {"id": "five_hour", "label": "5h", "used_percent": 42.2, "resets_at": NOW + 3600},
        {"id": "seven_day", "label": "wk", "used_percent": 18.0, "resets_at": NOW + 86400 * 3},
    ]


@pytest.mark.parametrize("payload", [None, [], {"five_hour": "x"}, {"five_hour": {}}])
def test_claude_status_line_malformed(payload: object) -> None:
    assert windows_from_claude_status_line(payload) == []


def test_claude_sdk_info() -> None:
    assert window_from_claude_sdk_info("five_hour", 0.5, NOW) == {
        "id": "five_hour",
        "label": "5h",
        "used_percent": 50.0,
        "resets_at": NOW,
    }
    assert window_from_claude_sdk_info("overage", 0.5, NOW) is None
    assert window_from_claude_sdk_info("seven_day", None, NOW) is None


def test_codex_camel_and_snake_case() -> None:
    camel = windows_from_codex_rate_limits(
        {
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": NOW},
            "secondary": {"usedPercent": 150, "windowDurationMins": 10080},
        }
    )
    assert [(w["label"], w["used_percent"], w["resets_at"]) for w in camel] == [
        ("5h", 12.0, NOW),
        ("wk", 100.0, None),
    ]
    snake = windows_from_codex_rate_limits(
        {"primary": {"used_percent": 3, "window_minutes": 1440}}
    )
    assert snake[0]["label"] == "1d"


def test_antigravity_catalog() -> None:
    catalog = {
        "models": {
            "a": {"model": "M_OTHER", "quotaInfo": {"remainingFraction": 0.1}},
            "b": {
                "model": "M_PRO",
                "displayName": "Gemini 3 Pro (High)",
                "quotaInfo": {"remainingFraction": 0.75, "resetTime": "2027-01-15T08:00:00Z"},
            },
        }
    }
    windows = windows_from_antigravity_catalog(catalog, "M_PRO")
    reset = int(datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp())
    assert windows == [
        {"id": "M_PRO", "label": "Gemini 3 Pro", "used_percent": 25.0, "resets_at": reset}
    ]
    assert windows_from_antigravity_catalog(catalog, "M_MISSING") == []
    assert windows_from_antigravity_catalog({"models": {"b": {"model": "M_PRO"}}}, "M_PRO") == []


def test_cache_merges_windows_and_isolates_owners() -> None:
    cache = UsageLimitsCache()
    five = window_from_claude_sdk_info("five_hour", 0.2, NOW + 100)
    week = window_from_claude_sdk_info("seven_day", 0.1, NOW + 1000)
    assert five is not None and week is not None
    assert cache.record("alice", provider_report("claude", [five]), now=NOW)
    assert cache.record("alice", provider_report("claude", [week]), now=NOW + 5)
    assert not cache.record("alice", {"provider": "unknown", "windows": [five]})
    assert not cache.record("alice", {"provider": "claude", "windows": [{"id": 1}]})

    snap = cache.snapshot("alice", now=NOW + 10)
    assert [p["provider"] for p in snap] == ["claude"]
    assert snap[0]["updated_at"] == NOW + 5
    assert {w["id"] for w in snap[0]["windows"]} == {"five_hour", "seven_day"}  # type: ignore[union-attr]
    assert cache.snapshot("bob") == []


def test_cache_marks_elapsed_windows_unknown() -> None:
    cache = UsageLimitsCache()
    cache.record(
        "u",
        provider_report(
            "codex",
            windows_from_codex_rate_limits(
                {"primary": {"usedPercent": 80, "windowDurationMins": 300, "resetsAt": NOW}}
            ),
        ),
        now=NOW - 10,
    )
    (window,) = cache.snapshot("u", now=NOW + 1)[0]["windows"]  # type: ignore[misc]
    assert window["used_percent"] is None and window["resets_at"] is None


def test_format_limits_line() -> None:
    providers = [
        {
            "provider": "claude",
            "windows": [
                {"label": "5h", "used_percent": 42.4, "resets_at": NOW + 3600},
                {"label": "wk", "used_percent": None, "resets_at": None},
            ],
        },
        {"provider": "codex", "windows": [{"label": "5h", "used_percent": 7, "resets_at": None}]},
    ]
    line = format_limits_line(providers, now=NOW)
    clock = datetime.fromtimestamp(NOW + 3600, tz=timezone.utc).astimezone().strftime("%H:%M")
    assert line == f"Claude 5h 42% ↻{clock} · wk – │ Codex 5h 7%"
    assert format_limits_line(None) == ""
    assert format_limits_line([]) == ""


def test_limits_route_reads_local_owner_in_single_user_mode() -> None:
    USAGE_LIMITS_CACHE.clear()
    try:
        USAGE_LIMITS_CACHE.record(
            RESERVED_USER_LOCAL,
            provider_report(
                "claude",
                windows_from_claude_status_line(
                    {"five_hour": {"used_percentage": 30, "resets_at": 4_000_000_000}}
                ),
            ),
        )
        app = FastAPI()
        app.include_router(create_usage_router(MagicMock()), prefix="/v1")
        body = TestClient(app).get("/v1/usage/limits").json()
        assert body["providers"][0]["provider"] == "claude"
        assert body["providers"][0]["windows"] == [
            {"id": "five_hour", "label": "5h", "used_percent": 30.0, "resets_at": 4_000_000_000}
        ]
    finally:
        USAGE_LIMITS_CACHE.clear()


async def test_native_ingress_accepts_rate_limits_only(db_uri: str) -> None:
    from omnigent.server.routes._sessions.orchestration import _persist_external_session_usage
    from omnigent.server.schemas import SessionEventInput
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="limits", agent_id="0123456789abcdef0123456789abcdef")
    report = provider_report(
        "codex",
        windows_from_codex_rate_limits(
            {"primary": {"usedPercent": 64, "windowDurationMins": 300, "resetsAt": 4_000_000_000}}
        ),
    )
    USAGE_LIMITS_CACHE.clear()
    try:
        result = await _persist_external_session_usage(
            conv.id,
            SessionEventInput(type="external_session_usage", data={"rate_limits": [report]}),
            store,
        )
        assert result is None
        (provider,) = USAGE_LIMITS_CACHE.snapshot(RESERVED_USER_LOCAL)
        assert provider["provider"] == "codex"
        assert provider["windows"][0]["used_percent"] == 64.0  # type: ignore[index]
    finally:
        USAGE_LIMITS_CACHE.clear()
