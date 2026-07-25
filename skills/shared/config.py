"""Configuration loading for CodeBlue AI skills.

Provides a unified configuration interface for all skills.
Configuration can be loaded from environment variables,
a config file, or defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CodeBlueConfig:
    """Configuration for CodeBlue AI."""

    # AWS region for operations
    region: str = "us-east-1"

    # Slack configuration
    slack_channel: str = ""
    slack_enabled: bool = True

    # Timeout configuration (seconds)
    skill_timeout: float = 15.0
    total_budget: float = 45.0
    mcp_connection_timeout: float = 30.0

    # Baseline configuration
    baseline_window_days: int = 7
    extended_baseline_window_days: int = 30

    # Lookback windows
    deploy_correlation_lookback_hours: int = 24
    log_triage_window_hours: int = 1
    event_window_hours: int = 1
    max_events: int = 100

    # Retry configuration
    slack_retry_max_attempts: int = 3
    slack_retry_base_delay: float = 1.0

    # Team configuration
    team_timezone: str = "UTC"
    business_hours_start: int = 9
    business_hours_end: int = 17

    # Service criticality (tier-1 services)
    tier1_services: list[str] = field(default_factory=list)

    # Additional settings
    extra: dict[str, Any] = field(default_factory=dict)


def load_config(config_path: str | Path | None = None) -> CodeBlueConfig:
    """Load CodeBlue AI configuration.

    Configuration is loaded from (in order of precedence):
    1. Environment variables (CODEBLUE_ prefix)
    2. Config file (if path provided)
    3. Defaults

    Args:
        config_path: Optional path to a JSON configuration file.

    Returns:
        Populated CodeBlueConfig instance.
    """
    config = CodeBlueConfig()

    # Load from config file if provided
    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                file_config = json.load(f)
            _apply_file_config(config, file_config)

    # Override with environment variables
    _apply_env_config(config)

    return config


def _apply_file_config(config: CodeBlueConfig, file_config: dict[str, Any]) -> None:
    """Apply configuration from a JSON file."""
    if "region" in file_config:
        config.region = file_config["region"]
    if "slack_channel" in file_config:
        config.slack_channel = file_config["slack_channel"]
    if "slack_enabled" in file_config:
        config.slack_enabled = file_config["slack_enabled"]
    if "skill_timeout" in file_config:
        config.skill_timeout = float(file_config["skill_timeout"])
    if "total_budget" in file_config:
        config.total_budget = float(file_config["total_budget"])
    if "team_timezone" in file_config:
        config.team_timezone = file_config["team_timezone"]
    if "tier1_services" in file_config:
        config.tier1_services = file_config["tier1_services"]
    if "baseline_window_days" in file_config:
        config.baseline_window_days = int(file_config["baseline_window_days"])
    if "deploy_correlation_lookback_hours" in file_config:
        config.deploy_correlation_lookback_hours = int(
            file_config["deploy_correlation_lookback_hours"]
        )
    # Store any additional keys
    known_keys = {
        "region", "slack_channel", "slack_enabled", "skill_timeout",
        "total_budget", "team_timezone", "tier1_services",
        "baseline_window_days", "deploy_correlation_lookback_hours",
    }
    for key, value in file_config.items():
        if key not in known_keys:
            config.extra[key] = value


def _apply_env_config(config: CodeBlueConfig) -> None:
    """Apply configuration from environment variables."""
    env_prefix = "CODEBLUE_"

    if region := os.environ.get(f"{env_prefix}REGION"):
        config.region = region
    if channel := os.environ.get(f"{env_prefix}SLACK_CHANNEL"):
        config.slack_channel = channel
    if enabled := os.environ.get(f"{env_prefix}SLACK_ENABLED"):
        config.slack_enabled = enabled.lower() in ("true", "1", "yes")
    if timeout := os.environ.get(f"{env_prefix}SKILL_TIMEOUT"):
        config.skill_timeout = float(timeout)
    if budget := os.environ.get(f"{env_prefix}TOTAL_BUDGET"):
        config.total_budget = float(budget)
    if tz := os.environ.get(f"{env_prefix}TEAM_TIMEZONE"):
        config.team_timezone = tz
    if services := os.environ.get(f"{env_prefix}TIER1_SERVICES"):
        config.tier1_services = [s.strip() for s in services.split(",") if s.strip()]
