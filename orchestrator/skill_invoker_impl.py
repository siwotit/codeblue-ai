"""Production SkillInvoker implementation for CodeBlue AI.

Invokes skill scripts via subprocess using `uv run`. Each skill is executed
as a separate process: input is passed as JSON on stdin, output is read as
JSON from stdout.

This is the production binding of the SkillInvoker interface defined in
workflow_orchestrator.py.

Requirements: 14.4 (executable via uv run), 12.3 (15s timeout enforcement)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from orchestrator.workflow_orchestrator import SkillInvoker, SkillResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill directory layout
# ---------------------------------------------------------------------------

# Default skills root relative to project root
DEFAULT_SKILLS_DIR = "skills"

# Mapping from skill names (as used by the orchestrator) to their script paths
# Each skill lives in skills/<skill-name>/<script_name>.py
SKILL_SCRIPT_MAP: dict[str, str] = {
    "alert-ingestion": "alert-ingestion/alert_ingestion.py",
    "eks-triage": "eks-triage/eks_triage.py",
    "ec2-triage": "ec2-triage/ec2_triage.py",
    "ecs-triage": "ecs-triage/ecs_triage.py",
    "hypothesis-engine": "hypothesis-engine/hypothesis_engine.py",
    "escalation-decision": "escalation-decision/escalation_decision.py",
    "incident-summary": "incident-summary-format/incident_summary.py",
}


class SubprocessSkillInvoker(SkillInvoker):
    """Production SkillInvoker that runs skill scripts via `uv run`.

    Each skill invocation:
    1. Locates the skill script in the skills/ directory
    2. Serializes input_data to JSON
    3. Runs `uv run <script>` with JSON on stdin
    4. Parses JSON output from stdout
    5. Returns SkillResult with success/failure

    Requirements:
        14.4: Scripts executable via `uv run` without pre-installed deps
        12.3: Timeout enforcement per skill invocation
    """

    def __init__(
        self,
        skills_dir: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        """Initialize the subprocess skill invoker.

        Args:
            skills_dir: Path to the skills directory. Defaults to <project_root>/skills.
            project_root: Project root directory. Defaults to the directory
                containing this file's parent (i.e., the repo root).
        """
        if project_root is None:
            # Assume orchestrator/ is one level below project root
            project_root = Path(__file__).resolve().parent.parent

        self._project_root = Path(project_root)

        if skills_dir is None:
            self._skills_dir = self._project_root / DEFAULT_SKILLS_DIR
        else:
            self._skills_dir = Path(skills_dir)

    @property
    def skills_dir(self) -> Path:
        """The configured skills directory."""
        return self._skills_dir

    def _resolve_script_path(self, skill_name: str) -> Path:
        """Resolve the script path for a skill name.

        Args:
            skill_name: The skill name (e.g., "metric-baseline").

        Returns:
            Absolute path to the skill script.

        Raises:
            FileNotFoundError: If the skill script does not exist.
        """
        relative_path = SKILL_SCRIPT_MAP.get(skill_name)
        if relative_path is None:
            raise FileNotFoundError(
                f"Unknown skill: '{skill_name}'. "
                f"Known skills: {sorted(SKILL_SCRIPT_MAP.keys())}"
            )

        script_path = self._skills_dir / relative_path
        if not script_path.exists():
            raise FileNotFoundError(
                f"Skill script not found: {script_path} "
                f"(skill: '{skill_name}')"
            )
        return script_path

    async def invoke(
        self,
        skill_name: str,
        input_data: dict[str, Any],
        timeout: float,
    ) -> SkillResult:
        """Invoke a skill script via subprocess.

        Runs `uv run <script_path>` with input_data as JSON on stdin.
        Captures stdout as JSON output.

        Args:
            skill_name: Name of the skill to invoke.
            input_data: JSON-serializable input for the skill.
            timeout: Maximum seconds to wait for completion.

        Returns:
            SkillResult with success/failure and output data.
        """
        start_time = time.monotonic()

        try:
            script_path = self._resolve_script_path(skill_name)
        except FileNotFoundError as e:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Serialize input to JSON
        try:
            input_json = json.dumps(input_data, default=str)
        except (TypeError, ValueError) as e:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Failed to serialize input: {e}",
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Run the skill via uv run
        cmd = ["uv", "run", str(script_path)]
        logger.debug("Invoking skill %s: %s", skill_name, " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=input_json.encode("utf-8")),
                timeout=timeout,
            )

            duration_ms = (time.monotonic() - start_time) * 1000

            if process.returncode != 0:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                return SkillResult(
                    skill_name=skill_name,
                    success=False,
                    error=f"Process exited with code {process.returncode}: {stderr_text[:500]}",
                    duration_ms=duration_ms,
                )

            # Parse stdout as JSON
            stdout_text = stdout_bytes.decode("utf-8").strip()
            if not stdout_text:
                return SkillResult(
                    skill_name=skill_name,
                    success=False,
                    error="Skill produced no output",
                    duration_ms=duration_ms,
                )

            try:
                output_data = json.loads(stdout_text)
            except json.JSONDecodeError as e:
                return SkillResult(
                    skill_name=skill_name,
                    success=False,
                    error=f"Invalid JSON output: {e}",
                    duration_ms=duration_ms,
                )

            # Check for error status in output
            if isinstance(output_data, dict) and output_data.get("status") == "error":
                return SkillResult(
                    skill_name=skill_name,
                    success=False,
                    data=output_data,
                    error=output_data.get("error", "Skill reported error status"),
                    duration_ms=duration_ms,
                )

            return SkillResult(
                skill_name=skill_name,
                success=True,
                data=output_data,
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start_time) * 1000
            # Kill the process if it's still running
            if process.returncode is None:
                process.kill()
                await process.wait()
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Skill timed out after {timeout:.1f}s",
                duration_ms=duration_ms,
            )

        except OSError as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Failed to start process: {e}",
                duration_ms=duration_ms,
            )
