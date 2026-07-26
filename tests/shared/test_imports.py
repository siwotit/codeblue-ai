"""Smoke tests to verify project scaffolding and imports work correctly."""

from skills.shared.config import CodeBlueConfig, load_config
from skills.shared.mcp_client import MCPClient, MCPResponse
from skills.shared.models import (
    AlertSource,
    AlertState,
    NormalizedAlert,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
)


def test_config_loads_defaults():
    """Verify default configuration loads without errors."""
    config = load_config()
    assert isinstance(config, CodeBlueConfig)
    assert config.region == "us-east-1"
    assert config.skill_timeout == 15.0
    assert config.total_budget == 45.0


def test_mcp_client_stub_returns_error():
    """Verify MCP client stub returns error in standalone mode."""
    client = MCPClient("test-mcp")
    response = client.invoke("test_tool", {"key": "value"})
    assert isinstance(response, MCPResponse)
    assert response.success is False
    assert response.error is not None


def test_normalized_alert_creation():
    """Verify NormalizedAlert can be instantiated with required fields."""
    from datetime import datetime, timezone

    alert = NormalizedAlert(
        id="test-alert-001",
        source=AlertSource.CLOUDWATCH,
        severity=Severity.HIGH,
        title="Test Alert",
        fired_at=datetime.now(timezone.utc),
        affected_resources=[
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:123456789:instance/i-abc123",
                display_name="i-abc123",
            )
        ],
        region="us-east-1",
        raw_payload={"original": "data"},
    )
    assert alert.id == "test-alert-001"
    assert alert.source == AlertSource.CLOUDWATCH
    assert alert.severity == Severity.HIGH
    assert len(alert.affected_resources) == 1
