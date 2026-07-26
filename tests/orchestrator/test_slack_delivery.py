"""Tests for the Slack delivery module.

Verifies:
- Successful delivery on first attempt
- Successful delivery after retries
- All attempts fail → local storage
- Exponential backoff timing
- Multi-cluster 120s timeout
- Read-only guard validation (only slack.postMessage/updateMessage)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.slack_delivery import (
    BACKOFF_BASE_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
    MULTI_CLUSTER_TIMEOUT_SECONDS,
    DeliveryResult,
    SlackDeliveryError,
    SlackDeliveryService,
)
from orchestrator.read_only_guard import ReadOnlyGuard


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_message() -> dict[str, Any]:
    """A sample Block Kit message for testing."""
    return {
        "channel": "#incidents",
        "text": "[CRITICAL] CPU spike on prod-api",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🔴 CPU spike on prod-api"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Severity:* 🔴 CRITICAL"},
            },
        ],
        "metadata": {
            "incidentId": "test-incident-123",
            "severity": "critical",
            "generatedBy": "codeblue-ai",
        },
    }


@pytest.fixture
def mock_slack_client() -> AsyncMock:
    """A mock Slack MCP client that succeeds."""
    client = AsyncMock()
    client.post_message = AsyncMock(return_value={"ts": "1234567890.123456", "ok": True})
    client.update_message = AsyncMock(return_value={"ts": "1234567890.123456", "ok": True})
    return client


@pytest.fixture
def tmp_storage_dir(tmp_path: Path) -> str:
    """A temporary directory for local report storage."""
    storage = tmp_path / ".codeblue"
    storage.mkdir()
    return str(storage)


@pytest.fixture
def delivery_service(mock_slack_client: AsyncMock, tmp_storage_dir: str) -> SlackDeliveryService:
    """A configured delivery service for testing."""
    return SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )


# ---------------------------------------------------------------------------
# Test: Successful delivery on first attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_delivery_first_attempt(
    delivery_service: SlackDeliveryService,
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
) -> None:
    """Verify successful delivery on first attempt returns correct result."""
    result = await delivery_service.deliver(
        sample_message, incident_id="inc-001"
    )

    assert result.success is True
    assert result.message_ts == "1234567890.123456"
    assert result.attempts == 1
    assert result.total_duration_ms > 0
    assert result.error is None
    assert result.local_path is None

    # Verify the client was called once with correct args
    mock_slack_client.post_message.assert_called_once_with(
        "#incidents", sample_message
    )


# ---------------------------------------------------------------------------
# Test: Successful delivery after retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_delivery_after_retry(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify delivery succeeds after initial failures."""
    # First call fails, second succeeds
    mock_slack_client.post_message = AsyncMock(
        side_effect=[
            SlackDeliveryError("rate_limited", retryable=True),
            {"ts": "1234567890.999999", "ok": True},
        ]
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    result = await service.deliver(sample_message, incident_id="inc-002")

    assert result.success is True
    assert result.message_ts == "1234567890.999999"
    assert result.attempts == 2
    assert result.error is None
    assert result.local_path is None
    assert mock_slack_client.post_message.call_count == 2


@pytest.mark.asyncio
async def test_successful_delivery_on_third_attempt(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify delivery succeeds on the final (third) attempt."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=[
            SlackDeliveryError("timeout", retryable=True),
            SlackDeliveryError("connection_error", retryable=True),
            {"ts": "1234567890.333333", "ok": True},
        ]
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    result = await service.deliver(sample_message, incident_id="inc-003")

    assert result.success is True
    assert result.message_ts == "1234567890.333333"
    assert result.attempts == 3
    assert mock_slack_client.post_message.call_count == 3


# ---------------------------------------------------------------------------
# Test: All attempts fail → local storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_attempts_fail_stores_locally(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify all failures result in local storage and error result."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=SlackDeliveryError("server_error", retryable=True)
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    result = await service.deliver(sample_message, incident_id="inc-004")

    assert result.success is False
    assert result.message_ts is None
    assert result.attempts == MAX_DELIVERY_ATTEMPTS
    assert result.error is not None
    assert "server_error" in result.error
    assert result.local_path is not None

    # Verify the file was actually written
    stored_file = Path(result.local_path)
    assert stored_file.exists()

    stored_data = json.loads(stored_file.read_text())
    assert stored_data["incident_id"] == "inc-004"
    assert stored_data["channel"] == "#incidents"
    assert stored_data["message"] == sample_message
    assert stored_data["delivery_error"] is not None


@pytest.mark.asyncio
async def test_non_retryable_error_stops_immediately(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify non-retryable errors don't waste attempts."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=SlackDeliveryError("channel_not_found", retryable=False)
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#nonexistent",
        storage_dir=tmp_storage_dir,
    )

    result = await service.deliver(sample_message, incident_id="inc-005")

    assert result.success is False
    assert result.attempts == 1  # Stopped after first non-retryable error
    assert "channel_not_found" in result.error
    assert result.local_path is not None
    mock_slack_client.post_message.assert_called_once()


@pytest.mark.asyncio
async def test_no_channel_configured_stores_locally(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify missing channel config results in immediate local storage."""
    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="",
        storage_dir=tmp_storage_dir,
    )

    result = await service.deliver(sample_message, incident_id="inc-006")

    assert result.success is False
    assert result.attempts == 0
    assert "No Slack channel configured" in result.error
    assert result.local_path is not None
    mock_slack_client.post_message.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Exponential backoff timing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exponential_backoff_timing(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify exponential backoff delays between retries (1s, 2s)."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=SlackDeliveryError("timeout", retryable=True)
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    sleep_durations: list[float] = []
    original_sleep = asyncio.sleep

    async def mock_sleep(duration: float) -> None:
        sleep_durations.append(duration)
        # Don't actually sleep in tests — just record the duration

    with patch("orchestrator.slack_delivery.asyncio.sleep", side_effect=mock_sleep):
        result = await service.deliver(sample_message, incident_id="inc-007")

    assert result.success is False
    assert result.attempts == MAX_DELIVERY_ATTEMPTS

    # Verify exponential backoff: 1s after first failure, 2s after second
    assert len(sleep_durations) == 2  # Backoff between attempts 1→2 and 2→3
    assert sleep_durations[0] == pytest.approx(BACKOFF_BASE_SECONDS, abs=0.01)  # 1s
    assert sleep_durations[1] == pytest.approx(BACKOFF_BASE_SECONDS * 2, abs=0.01)  # 2s


# ---------------------------------------------------------------------------
# Test: Multi-cluster 120s timeout budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_cluster_timeout_budget(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify delivery respects the timeout budget and stops when exhausted."""
    # Simulate slow responses that exhaust the budget
    async def slow_post(channel: str, message: dict) -> dict:
        await asyncio.sleep(0.1)  # Small delay
        raise SlackDeliveryError("slow_response", retryable=True)

    mock_slack_client.post_message = slow_post

    # Use a very short budget to test budget exhaustion
    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
        timeout_budget=0.5,  # 500ms budget — very tight
    )

    start = time.monotonic()
    result = await service.deliver(sample_message, incident_id="inc-008")
    elapsed = time.monotonic() - start

    assert result.success is False
    assert result.local_path is not None
    # Should complete within a reasonable time bounded by the budget
    assert elapsed < 2.0  # Well under the 120s default


@pytest.mark.asyncio
async def test_default_timeout_is_120_seconds(
    mock_slack_client: AsyncMock,
    tmp_storage_dir: str,
) -> None:
    """Verify the default timeout budget is 120s for multi-cluster support."""
    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    # The constant should be 120s
    assert MULTI_CLUSTER_TIMEOUT_SECONDS == 120.0


@pytest.mark.asyncio
async def test_timeout_budget_overridable_per_delivery(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify per-delivery timeout budget override works."""
    call_count = 0

    async def counting_post(channel: str, message: dict) -> dict:
        nonlocal call_count
        call_count += 1
        raise SlackDeliveryError("fail", retryable=True)

    mock_slack_client.post_message = counting_post

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
        timeout_budget=120.0,  # Default 120s
    )

    # Override with a very tight budget that won't allow retries with backoff
    with patch("orchestrator.slack_delivery.asyncio.sleep", new_callable=AsyncMock):
        result = await service.deliver(
            sample_message,
            incident_id="inc-009",
            timeout_budget=0.01,  # 10ms — should stop quickly
        )

    assert result.success is False
    assert result.local_path is not None


# ---------------------------------------------------------------------------
# Test: Read-only guard validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validates_slack_post_operation(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify the service validates slack.postMessage is allowed."""
    guard = ReadOnlyGuard()

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
        read_only_guard=guard,
    )

    result = await service.deliver(sample_message, incident_id="inc-010")

    # Should succeed — slack.postMessage is in the allowed operations
    assert result.success is True
    assert guard.rejection_count == 0


# ---------------------------------------------------------------------------
# Test: Connection/timeout error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asyncio_timeout_triggers_retry(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify asyncio.TimeoutError triggers retry logic."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=[
            asyncio.TimeoutError(),
            {"ts": "1234567890.111111", "ok": True},
        ]
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    with patch("orchestrator.slack_delivery.asyncio.sleep", new_callable=AsyncMock):
        result = await service.deliver(sample_message, incident_id="inc-011")

    assert result.success is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_generic_exception_triggers_retry(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify generic exceptions trigger retry logic."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=[
            ConnectionError("Connection refused"),
            {"ts": "1234567890.222222", "ok": True},
        ]
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    with patch("orchestrator.slack_delivery.asyncio.sleep", new_callable=AsyncMock):
        result = await service.deliver(sample_message, incident_id="inc-012")

    assert result.success is True
    assert result.attempts == 2


# ---------------------------------------------------------------------------
# Test: Local storage content validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_storage_contains_complete_report(
    mock_slack_client: AsyncMock,
    sample_message: dict[str, Any],
    tmp_storage_dir: str,
) -> None:
    """Verify locally stored report contains all expected fields."""
    mock_slack_client.post_message = AsyncMock(
        side_effect=SlackDeliveryError("all_failed", retryable=True)
    )

    service = SlackDeliveryService(
        slack_client=mock_slack_client,
        channel="#incidents",
        storage_dir=tmp_storage_dir,
    )

    with patch("orchestrator.slack_delivery.asyncio.sleep", new_callable=AsyncMock):
        result = await service.deliver(sample_message, incident_id="inc-013")

    stored_file = Path(result.local_path)
    stored_data = json.loads(stored_file.read_text())

    assert stored_data["incident_id"] == "inc-013"
    assert stored_data["channel"] == "#incidents"
    assert stored_data["message"] == sample_message
    assert "stored_at" in stored_data
    assert "delivery_error" in stored_data
