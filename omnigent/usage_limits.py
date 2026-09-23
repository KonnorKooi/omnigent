"""Subscription usage-limit windows reported passively by harness CLIs.

Claude Code and Codex report plan quota (e.g. the rolling 5-hour and weekly
windows) alongside normal turn traffic. This module normalizes those payloads
into one wire shape, caches the latest value per owner/provider on the server,
and formats a compact one-line summary for the terminal toolbar.

Wire shape of one provider report::

    {"provider": "claude",
     "windows": [{"id": "five_hour", "label": "5h",
                  "used_percent": 42.0, "resets_at": 1767225600}]}
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_ANTIGRAVITY = "antigravity"

_PROVIDER_ORDER = (PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_ANTIGRAVITY)
_PROVIDER_DISPLAY = {
    PROVIDER_CLAUDE: "Claude",
    PROVIDER_CODEX: "Codex",
    PROVIDER_ANTIGRAVITY: "Antigravity",
}

# Claude window ids (statusLine keys / SDK ``rateLimitType``) worth showing.
_CLAUDE_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "wk"}

Window = dict[str, Any]  # type: ignore[explicit-any]  # JSON wire dict


def _number(value: object) -> float | None:
    """Return *value* as a float when it is a real (non-bool) number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value: object) -> int | None:
    """Return a positive epoch-seconds int from *value*, else ``None``."""
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number)


def _window(window_id: str, label: str, used_percent: float, resets_at: int | None) -> Window:
    """Build one normalized window dict, clamping the percentage to 0-100."""
    return {
        "id": window_id,
        "label": label,
        "used_percent": round(min(max(used_percent, 0.0), 100.0), 1),
        "resets_at": resets_at,
    }


def windows_from_claude_status_line(rate_limits: object) -> list[Window]:
    """
    Parse the ``rate_limits`` block of Claude Code's statusLine stdin.

    :param rate_limits: e.g. ``{"five_hour": {"used_percentage": 23.5,
        "resets_at": 1767225600}, "seven_day": {...}}``.
    :returns: Normalized windows (empty when absent or malformed).
    """
    if not isinstance(rate_limits, Mapping):
        return []
    windows: list[Window] = []
    for window_id, label in _CLAUDE_WINDOW_LABELS.items():
        entry = rate_limits.get(window_id)
        if not isinstance(entry, Mapping):
            continue
        used = _number(entry.get("used_percentage"))
        if used is None:
            continue
        windows.append(_window(window_id, label, used, _epoch(entry.get("resets_at"))))
    return windows


def window_from_claude_sdk_info(
    rate_limit_type: object, utilization: object, resets_at: object
) -> Window | None:
    """
    Parse one Claude Agent SDK ``RateLimitInfo``.

    :param rate_limit_type: e.g. ``"five_hour"``.
    :param utilization: Fraction consumed, ``0.0``-``1.0``.
    :param resets_at: Unix seconds when the window resets.
    :returns: A normalized window, or ``None`` for untracked/malformed info.
    """
    if not isinstance(rate_limit_type, str) or rate_limit_type not in _CLAUDE_WINDOW_LABELS:
        return None
    fraction = _number(utilization)
    if fraction is None:
        return None
    return _window(
        rate_limit_type,
        _CLAUDE_WINDOW_LABELS[rate_limit_type],
        fraction * 100.0,
        _epoch(resets_at),
    )


def _codex_label(window_minutes: float | None, fallback: str) -> str:
    """Short label for a Codex window length, e.g. ``300`` -> ``"5h"``."""
    if window_minutes is None or window_minutes <= 0:
        return fallback
    if window_minutes == 10080:
        return "wk"
    if window_minutes % 1440 == 0:
        return f"{int(window_minutes // 1440)}d"
    if window_minutes % 60 == 0:
        return f"{int(window_minutes // 60)}h"
    return f"{int(window_minutes)}m"


def windows_from_codex_rate_limits(snapshot: object) -> list[Window]:
    """
    Parse a Codex ``RateLimitSnapshot`` (app-server camelCase or rollout snake_case).

    :param snapshot: e.g. ``{"primary": {"usedPercent": 12, "windowDurationMins":
        300, "resetsAt": 1767225600}, "secondary": {...}}``.
    :returns: Normalized ``primary`` / ``secondary`` windows.
    """
    if not isinstance(snapshot, Mapping):
        return []
    windows: list[Window] = []
    for window_id, fallback in (("primary", "5h"), ("secondary", "wk")):
        entry = snapshot.get(window_id)
        if not isinstance(entry, Mapping):
            continue
        used = _number(entry.get("usedPercent", entry.get("used_percent")))
        if used is None:
            continue
        minutes = _number(entry.get("windowDurationMins", entry.get("window_minutes")))
        resets_at = _epoch(entry.get("resetsAt", entry.get("resets_at")))
        windows.append(_window(window_id, _codex_label(minutes, fallback), used, resets_at))
    return windows


def _parse_reset_time(value: object) -> int | None:
    """Parse an ISO-8601 (``...Z``) or epoch-seconds reset time."""
    if isinstance(value, str) and value:
        try:
            return _epoch(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return _epoch(value)


def windows_from_antigravity_catalog(catalog: object, model_enum: str) -> list[Window]:
    """
    Parse the quota of one model from agy's ``GetAvailableModels`` catalog.

    :param catalog: e.g. ``{"models": {"k": {"model": "MODEL_X", "displayName":
        "Gemini 3 Pro (High)", "quotaInfo": {"remainingFraction": 0.8,
        "resetTime": "2026-01-01T00:00:00Z"}}}}``.
    :param model_enum: The enum of the model the turn ran on.
    :returns: One window for that model, or ``[]`` when it carries no quota.
    """
    models = catalog.get("models") if isinstance(catalog, Mapping) else None
    if not isinstance(models, Mapping):
        return []
    for entry in models.values():
        if not isinstance(entry, Mapping) or entry.get("model") != model_enum:
            continue
        quota = entry.get("quotaInfo")
        remaining = _number(quota.get("remainingFraction")) if isinstance(quota, Mapping) else None
        if remaining is None or not isinstance(quota, Mapping):
            return []
        display = entry.get("displayName")
        label = display.split(" (")[0] if isinstance(display, str) and display else "quota"
        return [
            _window(
                model_enum[:32],
                label[:24],
                (1.0 - remaining) * 100.0,
                _parse_reset_time(quota.get("resetTime")),
            )
        ]
    return []


def provider_report(provider: str, windows: list[Window]) -> dict[str, object] | None:
    """Wrap *windows* as a provider report, or ``None`` when there are none."""
    if not windows:
        return None
    return {"provider": provider, "windows": windows}


def _coerce_windows(raw: object) -> list[Window]:
    """Validate windows arriving over the wire; drop malformed entries."""
    if not isinstance(raw, list):
        return []
    windows: list[Window] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        window_id = entry.get("id")
        label = entry.get("label")
        used = _number(entry.get("used_percent"))
        if not isinstance(window_id, str) or not isinstance(label, str) or used is None:
            continue
        windows.append(_window(window_id[:32], label[:24], used, _epoch(entry.get("resets_at"))))
    return windows


class UsageLimitsCache:
    """Thread-safe latest-value cache keyed by (owner, provider), merged per window id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, object]]] = {}

    def record(self, owner: str, report: object, *, now: float | None = None) -> bool:
        """
        Merge one provider report into *owner*'s cache.

        :param owner: Owning user id (single-user mode uses a reserved sentinel).
        :param report: ``{"provider": str, "windows": [...]}`` from a harness.
        :returns: ``True`` when the report was valid and stored.
        """
        if not isinstance(report, Mapping):
            return False
        provider = report.get("provider")
        if provider not in _PROVIDER_DISPLAY:
            return False
        windows = _coerce_windows(report.get("windows"))
        if not windows:
            return False
        observed = int(now if now is not None else time.time())
        with self._lock:
            providers = self._data.setdefault(owner, {})
            entry = providers.setdefault(str(provider), {"windows": {}, "updated_at": observed})
            merged: dict[str, Window] = entry["windows"]  # type: ignore[assignment]
            for window in windows:
                merged[window["id"]] = window
            entry["updated_at"] = observed
        return True

    def snapshot(self, owner: str, *, now: float | None = None) -> list[dict[str, object]]:
        """
        Return *owner*'s providers in display order.

        A window whose reset time has passed reports ``used_percent=None``
        (usage restarted and no fresh reading has arrived yet).
        """
        current = now if now is not None else time.time()
        with self._lock:
            providers = {k: dict(v) for k, v in self._data.get(owner, {}).items()}
        out: list[dict[str, object]] = []
        for provider in _PROVIDER_ORDER:
            entry = providers.get(provider)
            if entry is None:
                continue
            windows: list[Window] = []
            for window in entry["windows"].values():  # type: ignore[union-attr]
                resets_at = window.get("resets_at")
                if isinstance(resets_at, int) and resets_at <= current:
                    window = {**window, "used_percent": None, "resets_at": None}
                windows.append(dict(window))
            out.append(
                {"provider": provider, "windows": windows, "updated_at": entry["updated_at"]}
            )
        return out

    def clear(self) -> None:
        """Drop every cached report (tests)."""
        with self._lock:
            self._data.clear()


USAGE_LIMITS_CACHE = UsageLimitsCache()


def _format_reset(resets_at: object, now: float) -> str:
    """Compact reset time: clock time within a day, weekday beyond."""
    if not isinstance(resets_at, int):
        return ""
    moment = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
    if resets_at - now < 24 * 3600:
        return moment.strftime("%H:%M")
    return moment.strftime("%a")


def format_limits_line(providers: object, *, now: float | None = None) -> str:
    """
    One-line toolbar summary, e.g. ``Claude 5h 42% ↻15:10 · wk 18% ↻Fri``.

    :param providers: ``providers`` list from ``GET /v1/usage/limits``.
    :returns: The summary, or ``""`` when there is nothing to show.
    """
    if not isinstance(providers, list):
        return ""
    current = now if now is not None else time.time()
    segments: list[str] = []
    for entry in providers:
        if not isinstance(entry, Mapping):
            continue
        name = _PROVIDER_DISPLAY.get(str(entry.get("provider")), str(entry.get("provider")))
        parts: list[str] = []
        windows = entry.get("windows")
        for window in windows if isinstance(windows, list) else []:
            if not isinstance(window, Mapping):
                continue
            used = _number(window.get("used_percent"))
            pct = f"{used:.0f}%" if used is not None else "–"
            reset = _format_reset(window.get("resets_at"), current)
            parts.append(f"{window.get('label')} {pct}" + (f" ↻{reset}" if reset else ""))
        if parts:
            segments.append(f"{name} " + " · ".join(parts))
    return " │ ".join(segments)
