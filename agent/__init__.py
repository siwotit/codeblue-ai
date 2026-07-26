"""CodeBlue AI agent configuration and system prompt.

Provides access to the system prompt that defines CodeBlue AI's persona,
read-only constraints, and workflow behavior.
"""

from __future__ import annotations

from pathlib import Path

# Path to the system prompt markdown file
SYSTEM_PROMPT_PATH: Path = Path(__file__).parent / "system_prompt.md"

# Agent identity constant used in metadata and Slack message footers
AGENT_IDENTITY: str = "CodeBlue AI"


def load_system_prompt() -> str:
    """Load the system prompt from disk.

    Returns:
        The full system prompt text as a string.

    Raises:
        FileNotFoundError: If the system_prompt.md file is missing.
    """
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
