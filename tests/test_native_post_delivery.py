"""Tests for the shared native-forwarder POST delivery classifier."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.native._native_post_delivery import (
    _DEAD_LETTER_BACKUP_FILE,
    _DEAD_LETTER_FILE,
    _DEAD_LETTER_MAX_BYTES,
    append_dead_letter,
    post_external_session_status,
    post_may_have_been_delivered,
)


def _read_records(path: Path) -> list[dict[str, object]]:
    """Parse a dead-letter file back into records."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    "exc,may_have_been_delivered",
    [
        # Server responded with a status — the events route returns 2xx
        # only after the append + consume publish, so any non-2xx means
        # the item was NOT committed. Safe to retry.
        (
            httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://test"),
                response=httpx.Response(503),
            ),
            False,
        ),
        # Connection never established / pool never acquired — no request
        # bytes were sent, so the item was not delivered. Safe to retry.
        (httpx.ConnectError("refused", request=httpx.Request("POST", "http://test")), False),
        (
            httpx.ConnectTimeout("slow connect", request=httpx.Request("POST", "http://test")),
            False,
        ),
        (httpx.PoolTimeout("no slot", request=httpx.Request("POST", "http://test")), False),
        # Auth flows can fail before yielding the request to the transport.
        # httpx leaves those RequestErrors unbound, proving no bytes were sent.
        (httpx.RequestError("auth failed before send"), False),
        # Request was sent and no response was seen — the server may have
        # committed it. Ambiguous: a retry could duplicate.
        (
            httpx.RequestError(
                "transport failed after binding",
                request=httpx.Request("POST", "http://test"),
            ),
            True,
        ),
        (httpx.ReadTimeout("no response", request=httpx.Request("POST", "http://test")), True),
        (httpx.WriteError("write failed", request=httpx.Request("POST", "http://test")), True),
        (
            httpx.RemoteProtocolError("peer closed", request=httpx.Request("POST", "http://test")),
            True,
        ),
    ],
)
def test_post_may_have_been_delivered_classification(
    exc: httpx.HTTPError, may_have_been_delivered: bool
) -> None:
    """
    Classify which POST failures may have reached + committed the server.

    A forwarder must not retry a POST that may already be committed,
    because external conversation items are not deduped server-side, so
    a retry would surface as a duplicate bubble in the web UI.
    A wrong classification means either duplicates (ambiguous error
    marked safe-to-retry) or lost messages (provably-undelivered error
    marked ambiguous and dropped).

    :param exc: HTTP exception raised while posting an AP event.
    :param may_have_been_delivered: Whether the request may have been
        committed despite the error.
    """
    assert post_may_have_been_delivered(exc) is may_have_been_delivered


def test_append_dead_letter_writes_parseable_line(tmp_path: Path) -> None:
    """
    A dropped forward payload is appended as one parseable JSON line.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message", "item_data": {"role": "assistant"}},
        reason="permanent HTTP failure after retries",
    )

    dl_path = tmp_path / _DEAD_LETTER_FILE
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_abc123"
    assert record["event_type"] == "external_conversation_item"
    assert record["reason"] == "permanent HTTP failure after retries"
    assert record["payload"] == {
        "item_type": "message",
        "item_data": {"role": "assistant"},
    }
    assert isinstance(record["ts"], (int, float))


def test_append_dead_letter_rotates_at_cap_keeping_newest(tmp_path: Path) -> None:
    """
    At the cap the file rotates to a ``.1`` backup and keeps the newest item.

    A sustained outage must retain the most recent drops, not stop at the
    oldest: the full file moves to ``dead_letter.jsonl.1`` and the new record
    lands in a fresh ``dead_letter.jsonl``.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    dl_path = tmp_path / _DEAD_LETTER_FILE
    backup_path = tmp_path / _DEAD_LETTER_BACKUP_FILE
    # Use a sparse file (truncate) to reach the cap without writing 50 MB.
    with dl_path.open("wb") as fh:
        fh.truncate(_DEAD_LETTER_MAX_BYTES + 1)
    capped_size = dl_path.stat().st_size

    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_session_usage",
        payload={"context_tokens": 1},
        reason="post failed",
    )

    # Old content rotated out to the backup; the newest record is in the
    # fresh active file as the sole line.
    assert backup_path.stat().st_size == capped_size
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "external_session_usage"
    assert record["payload"] == {"context_tokens": 1}


def test_append_dead_letter_never_raises_on_unwritable_dir() -> None:
    """A bogus / unwritable bridge dir is swallowed, not raised."""
    bogus = Path("/this/path/does/not/exist/and/cannot/be/made\x00")
    # Must return without raising despite the invalid path.
    append_dead_letter(
        bogus,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="post failed",
    )


def test_append_dead_letter_writes_classification_fields(tmp_path: Path) -> None:
    """
    Persist delivery classification for later forensic diagnosis.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="ambiguous transport failure (may already be committed)",
        delivered_ambiguous=True,
        http_status=None,
        transport_error="ReadTimeout",
    )

    record = _read_records(tmp_path / _DEAD_LETTER_FILE)[0]
    assert record["delivered_ambiguous"] is True
    assert record["http_status"] is None
    assert record["transport_error"] == "ReadTimeout"


def test_append_dead_letter_classification_defaults(tmp_path: Path) -> None:
    """A record written without classification args defaults to non-ambiguous."""
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_session_usage",
        payload={"context_tokens": 1},
        reason="post failed",
    )

    record = _read_records(tmp_path / _DEAD_LETTER_FILE)[0]
    assert record["delivered_ambiguous"] is False
    assert record["http_status"] is None
    assert record["transport_error"] is None


async def test_retry_loop_records_exhausted_connectivity_failure_for_watchdog() -> None:
    """An exhausted-retry connectivity failure is recorded for the idle watchdog.

    Writer half of issue #1119: when every POST attempt raises a connection
    error (e.g. ``No route to host``), the shared retry loop must record the
    failure in ``_native_forwarder_health`` so the harness idle-turn watchdog
    can name the real cause. Fails before the recording call was added (the
    health slot stays empty); passes after.
    """
    from omnigent.native import _native_forwarder_health as health
    from omnigent.native._native_post_delivery import post_session_event_with_retry

    class _AlwaysConnectError:
        """Stub client whose every POST fails to connect."""

        async def post(self, url: str, *, json: object) -> httpx.Response:
            """Raise a connect error mimicking an unreachable server."""
            del json
            raise httpx.ConnectError("No route to host", request=httpx.Request("POST", url))

    async def _no_sleep(_: float) -> None:
        """No-op sleep so retries don't add real delay."""

    health.clear()
    try:
        result = await post_session_event_with_retry(
            client=_AlwaysConnectError(),  # type: ignore[arg-type]
            url="/v1/sessions/conv_x/events",
            payload={"type": "external_session_status", "data": {}},
            event_type="external_session_status",
            max_attempts=2,
            retry_status_codes=frozenset(),
            sleep=_no_sleep,
            retry_delay=lambda _attempt: 0.0,
            logger_name="test.native_post_delivery",
        )
        assert result is None
        detail = health.recent_post_failure(60.0)
        assert detail is not None
        assert "external_session_status" in detail
        assert "No route to host" in detail
    finally:
        health.clear()


async def test_retry_loop_success_clears_a_prior_connectivity_failure() -> None:
    """A successful POST clears a previously recorded connectivity failure.

    Misattribution guard for issue #1119: once the server is reachable again,
    the retry loop must empty the failure slot so the idle watchdog can't blame
    a long-resolved outage for a later, unrelated stall.
    """
    from omnigent.native import _native_forwarder_health as health
    from omnigent.native._native_post_delivery import post_session_event_with_retry

    class _Ok:
        """Stub client whose POST always succeeds with 200."""

        async def post(self, url: str, *, json: object) -> httpx.Response:
            """Return a 200 response."""
            del json
            return httpx.Response(200, request=httpx.Request("POST", url))

    async def _no_sleep(_: float) -> None:
        """No-op sleep."""

    health.clear()
    try:
        # Simulate an earlier outage still on record.
        health.record_post_failure(
            "external_session_status", httpx.ConnectError("No route to host")
        )
        assert health.recent_post_failure(60.0) is not None
        response = await post_session_event_with_retry(
            client=_Ok(),  # type: ignore[arg-type]
            url="/v1/sessions/conv_x/events",
            payload={"type": "external_session_status", "data": {}},
            event_type="external_session_status",
            max_attempts=2,
            retry_status_codes=frozenset(),
            sleep=_no_sleep,
            retry_delay=lambda _attempt: 0.0,
            logger_name="test.native_post_delivery",
        )
        assert response is not None
        assert response.status_code == 200
        # The successful round-trip must have cleared the stale failure.
        assert health.recent_post_failure(60.0) is None
    finally:
        health.clear()


@pytest.mark.asyncio
async def test_post_external_session_status_attaches_failure_reason() -> None:
    """A failed status carries its reason as ``output``; idle omits it.

    The server surfaces a failed edge's ``output`` as the session's failure
    detail, so threading a drop reason there renders it instead of a bare
    "failed"; a normal idle edge sends a bare status with no output field.
    """
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_x/events":
            captured.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await post_external_session_status(
            client,
            session_id="conv_x",
            status="failed",
            output="transcript item item-1 rejected",
        )
        # No reason → no output field (e.g. a normal idle edge).
        await post_external_session_status(client, session_id="conv_x", status="idle")

    assert captured[0]["type"] == "external_session_status"
    assert captured[0]["data"] == {
        "status": "failed",
        "output": "transcript item item-1 rejected",
    }
    assert captured[1]["data"] == {"status": "idle"}
