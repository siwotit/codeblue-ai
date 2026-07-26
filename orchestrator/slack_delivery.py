"""CodeBlue AI Slack Delivery Module.

Delivers formatted Slack messages (Block Kit JSON) to a pre-configured
incident channel via the Slack MCP. Implements retry with exponential
backoff and local fallback storage on delivery failure.

Requirements: 7.4, 7.5, 7.6, 12.7
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from orchestrator.read_only_guard import ReadOnlyGuard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Retry configuration (Requirement 7.5)
MAX_DELIVERY_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0  # 1s → 2s → 4s exponential backoff

# Multi-cluster timeout budget (Requirement 12.7)
MULTI_CLUSTER_TIMEOUT_SECONDS = 120.0

# Default local storage directory for failed deliveries
DEFAULT_STORAGE_DIR = ".codeblue"

# Environment variable for Slack channel configuration
CHANNEL_ENV_VAR = "CODEBLUE_SLACK_CHANNEL"


# ---------------------------------------------------------------------------
# Slack MCP Interface
# ---------------------------------------------------------------------------


class SlackMCPClient(Protocol):
    """Protocol for the Slack MCP client.

    In production, this is implemented by the MCP connection layer.
    In tests, this can be replaced with a mock.
    """

    async def post_message(
        self, channel: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Post a message to a Slack channel.

        Args:
            channel: The Slack channel ID or name.
            message: The message payload (Block Kit JSON).

        Returns:
            Response from Slack API with at minimum a 'ts' field on success.

        Raises:
            SlackDeliveryError: If the Slack API rejects the message.
            Exception: On network or connection failures.
        """
        ...

    async def update_message(
        self, channel: str, ts: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing message in a Slack channel.

        Args:
            channel: The Slack channel ID or name.
            ts: The timestamp of the message to update.
            message: The updated message payload.

        Returns:
            Response from Slack API.

        Raises:
            SlackDeliveryError: If the Slack API rejects the update.
        """
        ...


# ---------------------------------------------------------------------------
# Error Types
# ---------------------------------------------------------------------------


class SlackDeliveryError(Exception):
    """Raised when Slack message delivery fails."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


# ---------------------------------------------------------------------------
# Delivery Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryResult:
    """Result of a Slack delivery attempt.

    Attributes:
        success: Whether the message was delivered successfully.
        message_ts: The Slack message timestamp (if successful).
        attempts: Number of delivery attempts made.
        total_duration_ms: Total time spent on delivery including retries.
        error: Error message if delivery failed.
        local_path: Path to locally stored report if all attempts failed.
    """

    success: bool
    message_ts: str | None = None
    attempts: int = 0
    total_duration_ms: float = 0.0
    error: str | None = None
    local_path: str | None = None


# ---------------------------------------------------------------------------
# Slack Delivery Service
# ---------------------------------------------------------------------------


class SlackDeliveryService:
    """Delivers incident reports to Slack with retry and fallback.

    Implements:
        - Exponential backoff: 1s → 2s → 4s, max 3 attempts (Req 7.5)
        - Local storage fallback on all-attempts-failed (Req 7.6)
        - Read-only guard validation for Slack operations (Req 10.3)
        - Multi-cluster 120s timeout budget (Req 12.7)

    Requirements:
        7.4: Post message to pre-configured incident channel via Slack MCP
        7.5: Retry with exponential backoff (1s base, doubling, max 3 attempts)
        7.6: Store report locally on all delivery failures, notify in session
        12.7: Support multi-cluster incidents with 120s timeout budget
    """

    def __init__(
        self,
        slack_client: SlackMCPClient,
        *,
        channel: str | None = None,
        storage_dir: str | None = None,
        read_only_guard: ReadOnlyGuard | None = None,
        timeout_budget: float = MULTI_CLUSTER_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the Slack delivery service.

        Args:
            slack_client: The Slack MCP client for posting messages.
            channel: Slack channel to post to. Defaults to CODEBLUE_SLACK_CHANNEL env.
            storage_dir: Directory for local report storage on failure.
            read_only_guard: Read-only enforcement guard. Creates one if not provided.
            timeout_budget: Maximum time budget for delivery (default 120s for multi-cluster).
        """
        self._client = slack_client
        self._channel = channel or os.environ.get(CHANNEL_ENV_VAR, "")
        self._storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self._guard = read_only_guard or ReadOnlyGuard()
        self._timeout_budget = timeout_budget

    @property
    def channel(self) -> str:
        """The configured Slack channel."""
        return self._channel

    async def deliver(
        self,
        message: dict[str, Any],
        *,
        incident_id: str = "unknown",
        timeout_budget: float | None = None,
    ) -> DeliveryResult:
        """Deliver a formatted Slack message with retry logic.

        Posts the message to the configured Slack channel. On failure,
        retries with exponential backoff (1s, 2s, 4s). If all attempts
        fail, stores the report locally and returns failure result.

        Args:
            message: The formatted Slack message (Block Kit JSON).
            incident_id: Incident ID for logging and local storage naming.
            timeout_budget: Optional override for the timeout budget in seconds.

        Returns:
            DeliveryResult with success/failure status and metadata.
        """
        if not self._channel:
            error_msg = (
                "No Slack channel configured. "
                f"Set {CHANNEL_ENV_VAR} environment variable."
            )
            logger.error(error_msg)
            local_path = self._store_locally(message, incident_id, error_msg)
            return DeliveryResult(
                success=False,
                attempts=0,
                error=error_msg,
                local_path=local_path,
            )

        # Validate that we're allowed to post to Slack
        self._guard.validate_invocation("slack.postMessage")

        budget = timeout_budget if timeout_budget is not None else self._timeout_budget
        start_time = time.monotonic()
        last_error: str = ""

        for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            elapsed = time.monotonic() - start_time
            remaining = budget - elapsed

            if remaining <= 0:
                last_error = (
                    f"Timeout budget exhausted ({budget:.1f}s) after "
                    f"{attempt - 1} attempt(s)"
                )
                logger.warning(
                    "Slack delivery timeout budget exhausted for incident %s",
                    incident_id,
                )
                break

            try:
                # Attempt delivery with remaining budget as timeout
                delivery_timeout = min(remaining, 30.0)  # Cap individual attempt at 30s
                response = await asyncio.wait_for(
                    self._client.post_message(self._channel, message),
                    timeout=delivery_timeout,
                )

                total_duration = (time.monotonic() - start_time) * 1000
                message_ts = response.get("ts", "")

                logger.info(
                    "Slack delivery succeeded for incident %s "
                    "(attempt %d, %.0fms, ts=%s)",
                    incident_id,
                    attempt,
                    total_duration,
                    message_ts,
                )

                return DeliveryResult(
                    success=True,
                    message_ts=message_ts,
                    attempts=attempt,
                    total_duration_ms=total_duration,
                )

            except asyncio.TimeoutError:
                last_error = f"Attempt {attempt}: timed out after {delivery_timeout:.1f}s"
                logger.warning(
                    "Slack delivery attempt %d timed out for incident %s",
                    attempt,
                    incident_id,
                )

            except SlackDeliveryError as e:
                last_error = f"Attempt {attempt}: {e}"
                logger.warning(
                    "Slack delivery attempt %d failed for incident %s: %s",
                    attempt,
                    incident_id,
                    e,
                )
                if not e.retryable:
                    # Non-retryable error — don't waste time on more attempts
                    break

            except Exception as e:
                last_error = f"Attempt {attempt}: {type(e).__name__}: {e}"
                logger.warning(
                    "Slack delivery attempt %d failed for incident %s: %s",
                    attempt,
                    incident_id,
                    e,
                )

            # Apply exponential backoff before next attempt (if not last)
            if attempt < MAX_DELIVERY_ATTEMPTS:
                backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                elapsed_after = time.monotonic() - start_time
                remaining_after = budget - elapsed_after

                if remaining_after <= backoff:
                    last_error = (
                        f"Timeout budget insufficient for backoff "
                        f"(need {backoff:.1f}s, have {remaining_after:.1f}s)"
                    )
                    logger.warning(
                        "Slack delivery: insufficient budget for backoff, "
                        "incident %s",
                        incident_id,
                    )
                    break

                await asyncio.sleep(backoff)

        # All attempts failed — store locally (Requirement 7.6)
        total_duration = (time.monotonic() - start_time) * 1000
        local_path = self._store_locally(message, incident_id, last_error)

        # Notify in session (Requirement 7.6)
        self._notify_session_failure(incident_id, local_path, last_error)

        return DeliveryResult(
            success=False,
            attempts=min(attempt, MAX_DELIVERY_ATTEMPTS),
            total_duration_ms=total_duration,
            error=last_error,
            local_path=local_path,
        )

    def _store_locally(
        self,
        message: dict[str, Any],
        incident_id: str,
        error: str,
    ) -> str:
        """Store the report locally for manual retrieval.

        Requirement 7.6: Store the report locally on all delivery failures.

        Args:
            message: The formatted Slack message to store.
            incident_id: The incident ID for file naming.
            error: The error that caused delivery failure.

        Returns:
            The path to the stored report file.
        """
        storage_path = Path(self._storage_dir)
        storage_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"incident_{incident_id}_{timestamp}.json"
        file_path = storage_path / filename

        report_data = {
            "incident_id": incident_id,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "delivery_error": error,
            "channel": self._channel,
            "message": message,
        }

        try:
            file_path.write_text(
                json.dumps(report_data, indent=2, default=str),
                encoding="utf-8",
            )
            logger.info(
                "Stored undelivered report locally: %s", file_path
            )
        except OSError as e:
            logger.error(
                "Failed to store report locally for incident %s: %s",
                incident_id,
                e,
            )
            return f"<storage failed: {e}>"

        return str(file_path)

    def _notify_session_failure(
        self,
        incident_id: str,
        local_path: str,
        error: str,
    ) -> None:
        """Notify in the Claude Code session about delivery failure.

        Requirement 7.6: Display a notification message in the Claude Code
        session indicating delivery failure and the local storage path.

        Args:
            incident_id: The incident ID.
            local_path: Path where the report was stored locally.
            error: The delivery error message.
        """
        logger.error(
            "⚠️  SLACK DELIVERY FAILED for incident %s\n"
            "   Error: %s\n"
            "   Report stored at: %s\n"
            "   Please deliver manually or retry.",
            incident_id,
            error,
            local_path,
        )
