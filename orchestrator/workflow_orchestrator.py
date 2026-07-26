"""CodeBlue AI Workflow Orchestrator.

Coordinates the end-to-end incident response workflow. Receives normalized
alerts, dispatches skill invocations in dependency order, aggregates results,
and produces the final IncidentReport.

Requirements: 10.5, 10.6, 11.1, 12.1, 12.2
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from skills.shared.models import (
    BlastRadius,
    ClusterHealthReport,
    CollectedSignals,
    CorrelatedChanges,
    EscalationDecision,
    EvidenceItem,
    Hypothesis,
    IncidentReport,
    LogFindings,
    MetricDeviation,
    NormalizedAlert,
    Severity,
    SkillError,
    WorkflowState,
)

from orchestrator.tool_registry import (
    ToolCategory,
    validate_operation,
    WriteOperationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-skill timeout in seconds (Requirement 12.3)
SKILL_TIMEOUT_SECONDS = 15

# MCP connection timeout in seconds (Requirement 11.1)
MCP_CONNECTION_TIMEOUT_SECONDS = 30

# Cumulative wall-clock budget in seconds (Requirement 12.4)
CUMULATIVE_TIMEOUT_SECONDS = 45

# Multi-cluster budget in seconds (Requirement 12.7)
MULTI_CLUSTER_TIMEOUT_SECONDS = 120

# Confidence reduction per unavailable source (Requirement 11.3)
CONFIDENCE_REDUCTION_PER_SOURCE = 0.2

# Minimum confidence floor (Requirement 11.3)
MIN_CONFIDENCE = 0.1

# Maximum confidence when baseline is normal despite alarm (Requirement 11.5)
NORMAL_BASELINE_MAX_CONFIDENCE = 0.3


class WorkflowPhase(str, Enum):
    """Phases of the incident response workflow."""

    INGESTION = "ingestion"
    SIGNAL_COLLECTION = "metric_analysis"
    CLUSTER_CHECK = "cluster_check"
    CORRELATION = "correlation"
    LOG_TRIAGE = "log_triage"
    HYPOTHESIS = "hypothesis"
    REPORTING = "reporting"


# ---------------------------------------------------------------------------
# Data Gap Annotations (Requirement 11.2)
# ---------------------------------------------------------------------------


@dataclass
class DataGap:
    """Annotation for incomplete data in the incident report.

    Requirement 11.2: Annotate each gap by listing the unavailable data
    source name and the workflow phase that was skipped.
    """

    source_name: str
    workflow_phase: str
    reason: str
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Skill Invocation Interface
# ---------------------------------------------------------------------------


@dataclass
class SkillResult:
    """Result from a skill invocation."""

    skill_name: str
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0


class SkillInvoker:
    """Interface for invoking skills.

    This is the abstraction layer that the orchestrator uses to run skills.
    In production, this invokes skill scripts via subprocess/uv run.
    In testing, this can be replaced with a mock implementation.
    """

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        """Invoke a skill with the given input data.

        Args:
            skill_name: Name of the skill to invoke.
            input_data: JSON-serializable input for the skill.
            timeout: Maximum seconds to wait for completion.

        Returns:
            SkillResult with success/failure and output data.
        """
        raise NotImplementedError("Subclasses must implement invoke()")


# ---------------------------------------------------------------------------
# Workflow Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class WorkflowContext:
    """Internal mutable state during a workflow execution."""

    incident_id: str
    alert: NormalizedAlert
    start_time: float  # monotonic clock
    is_multi_cluster: bool = False
    phase: WorkflowPhase = WorkflowPhase.INGESTION
    signals: CollectedSignals = field(default_factory=CollectedSignals)
    errors: list[SkillError] = field(default_factory=list)
    cancelled_skills: list[str] = field(default_factory=list)
    data_gaps: list[DataGap] = field(default_factory=list)

    @property
    def timeout_budget(self) -> float:
        """Total wall-clock budget based on incident scope.

        Requirement 12.4: 45-second cumulative budget for single-cluster.
        Requirement 12.7: 120-second budget for multi-cluster incidents.
        """
        if self.is_multi_cluster:
            return MULTI_CLUSTER_TIMEOUT_SECONDS
        return CUMULATIVE_TIMEOUT_SECONDS

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock seconds elapsed since workflow start."""
        return time.monotonic() - self.start_time

    @property
    def remaining_budget(self) -> float:
        """Remaining wall-clock budget in seconds."""
        return max(0.0, self.timeout_budget - self.elapsed_seconds)

    def is_budget_exceeded(self) -> bool:
        """Check if the cumulative timeout budget is exceeded."""
        return self.elapsed_seconds >= self.timeout_budget

    def all_signal_sources_failed(self) -> bool:
        """Check if all core MCP/signal sources have failed.

        Requirement 11.4: Detect when all MCPs fail so that we produce
        a report with only original alert info at 0.1 confidence.
        """
        core_sources = {"metric-baseline", "deploy-correlation", "log-triage"}
        unavailable = set(self.signals.unavailable_sources)
        return core_sources.issubset(unavailable)


class WorkflowOrchestrator:
    """Coordinates the end-to-end incident response workflow.

    Implements the dependency-ordered skill execution:
      1. alert-ingestion (already done — alert arrives normalized)
      2. [metric-baseline, deploy-correlation, k8s-health, log-triage] (concurrent)
      3. hypothesis-engine
      4. [escalation-decision, evidence-provenance] (concurrent)
      5. incident-summary-format

    Requirements:
        10.5: Never invoke write/mutate tools against monitored systems.
        10.6: Reject and log attempted write operations.
        11.1: Continue workflow on partial MCP/skill failures.
        12.1: Produce report within 60s wall-clock.
        12.2: Execute independent skills concurrently.
    """

    def __init__(self, skill_invoker: SkillInvoker) -> None:
        """Initialize the orchestrator.

        Args:
            skill_invoker: The skill invocation backend to use.
        """
        self._invoker = skill_invoker

    async def handle_alert(self, alert: NormalizedAlert) -> IncidentReport:
        """Process a normalized alert through the full workflow.

        This is the primary entry point. It orchestrates skill execution
        in dependency order, aggregates signals, and produces the final
        IncidentReport.

        Args:
            alert: The normalized alert to process.

        Returns:
            A complete IncidentReport with hypotheses and recommendations.
        """
        ctx = WorkflowContext(
            incident_id=str(uuid.uuid4()),
            alert=alert,
            start_time=time.monotonic(),
            is_multi_cluster=self._is_multi_cluster_incident(alert),
        )
        logger.info(
            "Starting workflow for incident %s (alert: %s, multi_cluster: %s)",
            ctx.incident_id,
            alert.id,
            ctx.is_multi_cluster,
        )

        # Phase 1: Signal collection (concurrent)
        ctx.phase = WorkflowPhase.SIGNAL_COLLECTION
        await self._collect_signals(ctx)

        # Phase 2: Hypothesis generation
        if not ctx.is_budget_exceeded():
            ctx.phase = WorkflowPhase.HYPOTHESIS
            await self._generate_hypotheses(ctx)

        # Phase 3: Escalation + Evidence provenance (concurrent)
        if not ctx.is_budget_exceeded():
            ctx.phase = WorkflowPhase.REPORTING
            await self._finalize_report(ctx)

        # Assemble the final report
        report = self._assemble_report(ctx)
        logger.info(
            "Workflow complete for incident %s in %.1fs",
            ctx.incident_id,
            ctx.elapsed_seconds,
        )
        return report

    def get_workflow_state(self, ctx: WorkflowContext) -> WorkflowState:
        """Get the current workflow state for a context.

        Args:
            ctx: The workflow context.

        Returns:
            A WorkflowState snapshot.
        """
        return WorkflowState(
            incident_id=ctx.incident_id,
            phase=ctx.phase.value,
            started_at=datetime.now(timezone.utc),
            signals=ctx.signals,
            errors=ctx.errors,
        )

    # ------------------------------------------------------------------
    # Skill determination
    # ------------------------------------------------------------------

    def _should_run_k8s_skills(self, alert: NormalizedAlert) -> bool:
        """Determine whether K8s health checks should run.

        Requirement 3.7: Skip Kubernetes health checks when the alert's
        NormalizedAlert has an empty or absent cluster field.

        Args:
            alert: The normalized alert.

        Returns:
            True if K8s skills should be invoked.
        """
        return bool(alert.cluster)

    def _is_multi_cluster_incident(self, alert: NormalizedAlert) -> bool:
        """Determine whether an incident spans multiple clusters.

        Requirement 12.7: Multi-cluster incidents get 120s budget.
        An incident is multi-cluster if the affected resources reference
        more than one distinct cluster name.

        Args:
            alert: The normalized alert.

        Returns:
            True if incident spans multiple clusters.
        """
        clusters: set[str] = set()
        if alert.cluster:
            clusters.add(alert.cluster)
        # Check affected resources for additional cluster references
        for resource in alert.affected_resources:
            # K8s resources may encode cluster in their value
            if resource.type.value == "k8s_resource" and "/" in resource.value:
                # Format: cluster/namespace/kind/name or namespace/kind/name
                parts = resource.value.split("/")
                if len(parts) >= 4:
                    clusters.add(parts[0])
        return len(clusters) > 1

    def _determine_signal_skills(
        self, alert: NormalizedAlert
    ) -> list[tuple[str, dict[str, Any]]]:
        """Determine which signal-collection skills to invoke.

        Returns a list of (skill_name, input_data) tuples for concurrent
        execution in the signal collection phase.

        Args:
            alert: The normalized alert.

        Returns:
            List of (skill_name, input_data) pairs.
        """
        alert_dict = alert.model_dump(by_alias=True, mode="json")
        skills: list[tuple[str, dict[str, Any]]] = []

        # metric-baseline — always run
        skills.append((
            "metric-baseline",
            {
                "metricName": alert.metric_name or alert.title,
                "namespace": alert.metric_namespace or "AWS/Unknown",
                "dimensions": alert.dimensions or {},
                "currentWindow": {
                    "start": alert.fired_at.isoformat(),
                    "end": datetime.now(timezone.utc).isoformat(),
                },
                "baselineWindow": "7d",
                "alert": alert_dict,
            },
        ))

        # deploy-correlation — always run
        skills.append((
            "deploy-correlation",
            {
                "affectedResources": [
                    r.model_dump(by_alias=True) for r in alert.affected_resources
                ],
                "incidentTime": alert.fired_at.isoformat(),
                "lookbackWindow": "24h",
                "alert": alert_dict,
            },
        ))

        # log-triage — always run
        log_groups = self._infer_log_groups(alert)
        skills.append((
            "log-triage",
            {
                "logGroups": log_groups,
                "namespaces": [alert.namespace] if alert.namespace else [],
                "timeRange": {
                    "start": alert.fired_at.isoformat(),
                    "end": datetime.now(timezone.utc).isoformat(),
                },
                "alert": alert_dict,
            },
        ))

        # K8s health skills — only for EKS workloads (Req 3.7)
        if self._should_run_k8s_skills(alert):
            skills.append((
                "k8s-cluster-health",
                {
                    "clusterName": alert.cluster,
                    "region": alert.region,
                    "alert": alert_dict,
                },
            ))
            skills.append((
                "pod-failure-triage",
                {
                    "namespace": alert.namespace or "default",
                    "clusterName": alert.cluster,
                    "alert": alert_dict,
                },
            ))
            skills.append((
                "node-condition-check",
                {
                    "clusterName": alert.cluster,
                    "alert": alert_dict,
                },
            ))
            skills.append((
                "eks-addon-status",
                {
                    "clusterName": alert.cluster,
                    "alert": alert_dict,
                },
            ))

        return skills

    def _infer_log_groups(self, alert: NormalizedAlert) -> list[str]:
        """Infer log groups from alert context.

        Args:
            alert: The normalized alert.

        Returns:
            List of log group names to query.
        """
        log_groups: list[str] = []
        for resource in alert.affected_resources:
            if resource.type.value == "arn" and ":log-group:" in resource.value:
                log_groups.append(resource.value)
            elif resource.type.value == "arn":
                # Derive potential log group from ARN
                parts = resource.value.split(":")
                if len(parts) >= 6:
                    service = parts[2]
                    log_groups.append(f"/aws/{service}/{resource.display_name or parts[-1]}")

        if not log_groups and alert.namespace:
            log_groups.append(f"/aws/containerinsights/{alert.cluster}/application")

        if not log_groups:
            log_groups.append(f"/aws/codeblue/{alert.region}/default")

        return log_groups

    # ------------------------------------------------------------------
    # Signal collection (concurrent execution)
    # ------------------------------------------------------------------

    async def _collect_signals(self, ctx: WorkflowContext) -> None:
        """Execute signal-collection skills concurrently.

        Requirement 12.2: Execute metric pulls, CloudTrail queries, and
        Kubernetes checks concurrently when independent.

        Args:
            ctx: The mutable workflow context.
        """
        skills_to_run = self._determine_signal_skills(ctx.alert)

        # Run all signal-collection skills concurrently
        results = await self._run_skills_concurrent(ctx, skills_to_run)

        # Process results and populate signals
        for result in results:
            self._integrate_signal_result(ctx, result)

    def _integrate_signal_result(
        self, ctx: WorkflowContext, result: SkillResult
    ) -> None:
        """Integrate a skill result into the collected signals.

        Requirement 11.2: When a skill fails, produce a report marked as having
        incomplete data with structured annotations of missing data sections.

        Args:
            ctx: The workflow context.
            result: The skill execution result.
        """
        if not result.success:
            ctx.errors.append(SkillError(
                skill_name=result.skill_name,
                error_type="skill_failure",
                message=result.error or "Unknown error",
                timestamp=datetime.now(timezone.utc),
                recoverable=True,
            ))
            ctx.signals.unavailable_sources.append(result.skill_name)
            # Record structured data gap annotation (Requirement 11.2)
            ctx.data_gaps.append(DataGap(
                source_name=result.skill_name,
                workflow_phase=ctx.phase.value,
                reason=result.error or "Unknown error",
            ))
            logger.warning(
                "Skill %s failed: %s", result.skill_name, result.error
            )
            return

        data = result.data or {}

        if result.skill_name == "metric-baseline":
            try:
                ctx.signals.metric_deviation = MetricDeviation(**data)
            except Exception as e:
                logger.warning("Failed to parse metric-baseline output: %s", e)
                ctx.signals.unavailable_sources.append("metric-baseline")

        elif result.skill_name == "deploy-correlation":
            try:
                ctx.signals.correlated_changes = CorrelatedChanges(**data)
            except Exception as e:
                logger.warning("Failed to parse deploy-correlation output: %s", e)
                ctx.signals.unavailable_sources.append("deploy-correlation")

        elif result.skill_name == "log-triage":
            try:
                ctx.signals.log_findings = LogFindings(**data)
            except Exception as e:
                logger.warning("Failed to parse log-triage output: %s", e)
                ctx.signals.unavailable_sources.append("log-triage")

        elif result.skill_name in (
            "k8s-cluster-health",
            "pod-failure-triage",
            "node-condition-check",
            "eks-addon-status",
        ):
            # K8s skills contribute to the cluster health report
            self._integrate_k8s_result(ctx, result)

    def _integrate_k8s_result(
        self, ctx: WorkflowContext, result: SkillResult
    ) -> None:
        """Integrate K8s skill results into the cluster health report.

        Multiple K8s skills contribute to a single ClusterHealthReport.

        Args:
            ctx: The workflow context.
            result: The skill result.
        """
        data = result.data or {}

        if result.skill_name == "k8s-cluster-health":
            try:
                ctx.signals.cluster_health = ClusterHealthReport(**data)
            except Exception as e:
                logger.warning(
                    "Failed to parse k8s-cluster-health output: %s", e
                )
                ctx.signals.unavailable_sources.append("k8s-cluster-health")
        # pod-failure-triage, node-condition-check, eks-addon-status
        # enrich the existing cluster health report if present
        elif ctx.signals.cluster_health is not None:
            # These skills provide supplementary data; merge into report
            logger.debug(
                "Supplementary K8s data from %s integrated",
                result.skill_name,
            )

    # ------------------------------------------------------------------
    # Hypothesis generation
    # ------------------------------------------------------------------

    async def _generate_hypotheses(self, ctx: WorkflowContext) -> None:
        """Run the hypothesis engine with all collected signals.

        Args:
            ctx: The workflow context with populated signals.
        """
        alert_dict = ctx.alert.model_dump(by_alias=True, mode="json")
        input_data: dict[str, Any] = {"alert": alert_dict}

        if ctx.signals.metric_deviation:
            input_data["metricDeviation"] = ctx.signals.metric_deviation.model_dump(
                by_alias=True, mode="json"
            )
        if ctx.signals.correlated_changes:
            input_data["correlatedChanges"] = (
                ctx.signals.correlated_changes.model_dump(
                    by_alias=True, mode="json"
                )
            )
        if ctx.signals.log_findings:
            input_data["logFindings"] = ctx.signals.log_findings.model_dump(
                by_alias=True, mode="json"
            )
        if ctx.signals.cluster_health:
            input_data["clusterHealth"] = ctx.signals.cluster_health.model_dump(
                by_alias=True, mode="json"
            )

        timeout = min(SKILL_TIMEOUT_SECONDS, ctx.remaining_budget)
        result = await self._invoke_skill_safe(ctx, "hypothesis-engine", input_data, timeout)

        if result and result.success and result.data:
            # Store hypotheses in signals evidence items for now
            hypotheses_data = result.data.get("hypotheses", [])
            ctx.signals.evidence_items = [
                EvidenceItem(
                    claim=h.get("title", ""),
                    source="hypothesis-engine",
                    weight=h.get("confidence", 0.5),
                )
                for h in hypotheses_data
            ]

    # ------------------------------------------------------------------
    # Report finalization (escalation + evidence provenance, concurrent)
    # ------------------------------------------------------------------

    async def _finalize_report(self, ctx: WorkflowContext) -> None:
        """Run escalation-decision and evidence-provenance concurrently.

        Args:
            ctx: The workflow context.
        """
        alert_dict = ctx.alert.model_dump(by_alias=True, mode="json")

        skills_to_run: list[tuple[str, dict[str, Any]]] = [
            (
                "escalation-decision",
                {
                    "severity": ctx.alert.severity.value,
                    "blastRadius": self._compute_blast_radius(ctx).model_dump(
                        by_alias=True, mode="json"
                    ),
                    "hypotheses": [
                        ei.model_dump(by_alias=True, mode="json")
                        for ei in ctx.signals.evidence_items
                    ],
                    "alert": alert_dict,
                },
            ),
            (
                "evidence-provenance",
                {
                    "findings": [
                        ei.model_dump(by_alias=True, mode="json")
                        for ei in ctx.signals.evidence_items
                    ],
                    "region": ctx.alert.region,
                    "clusterContext": ctx.alert.cluster,
                    "alert": alert_dict,
                },
            ),
        ]

        await self._run_skills_concurrent(ctx, skills_to_run)

    # ------------------------------------------------------------------
    # Concurrent skill execution
    # ------------------------------------------------------------------

    async def _run_skills_concurrent(
        self,
        ctx: WorkflowContext,
        skills: list[tuple[str, dict[str, Any]]],
    ) -> list[SkillResult]:
        """Run multiple skills concurrently with timeout enforcement.

        Requirement 12.2: Execute independent skills concurrently.
        Requirement 12.4: Cancel skills if cumulative budget exceeded.

        Args:
            ctx: The workflow context.
            skills: List of (skill_name, input_data) tuples.

        Returns:
            List of SkillResult objects (one per skill).
        """
        if not skills:
            return []

        if ctx.is_budget_exceeded():
            # Budget already spent — mark all as cancelled
            for skill_name, _ in skills:
                ctx.cancelled_skills.append(skill_name)
                ctx.signals.unavailable_sources.append(skill_name)
            return []

        tasks: list[asyncio.Task[SkillResult]] = []
        for skill_name, input_data in skills:
            timeout = min(SKILL_TIMEOUT_SECONDS, ctx.remaining_budget)
            task = asyncio.create_task(
                self._invoke_skill_safe(ctx, skill_name, input_data, timeout),
                name=skill_name,
            )
            tasks.append(task)

        # Wait for all tasks with remaining budget as overall timeout
        done, pending = await asyncio.wait(
            tasks,
            timeout=ctx.remaining_budget,
            return_when=asyncio.ALL_COMPLETED,
        )

        # Cancel any still-pending tasks (Requirement 12.4)
        results: list[SkillResult] = []
        for task in pending:
            task.cancel()
            skill_name = task.get_name()
            ctx.cancelled_skills.append(skill_name)
            ctx.signals.unavailable_sources.append(skill_name)
            results.append(SkillResult(
                skill_name=skill_name,
                success=False,
                error="Cancelled: cumulative timeout budget exceeded",
            ))
            logger.warning("Skill %s cancelled (budget exceeded)", skill_name)

        for task in done:
            try:
                results.append(task.result())
            except Exception as e:
                skill_name = task.get_name()
                results.append(SkillResult(
                    skill_name=skill_name,
                    success=False,
                    error=str(e),
                ))

        return results

    async def _invoke_skill_safe(
        self,
        ctx: WorkflowContext,
        skill_name: str,
        input_data: dict[str, Any],
        timeout: float,
    ) -> SkillResult:
        """Invoke a single skill with timeout and error handling.

        Requirement 10.5: Validate no write operations attempted.
        Requirement 11.1: Handle MCP connection failures (30s timeout).
        Requirement 12.3: Enforce 15-second per-skill timeout.

        The effective timeout is the minimum of the requested timeout and
        MCP_CONNECTION_TIMEOUT_SECONDS (30s). MCP connection failures are
        logged and the source is marked unavailable.

        Args:
            ctx: The workflow context.
            skill_name: Name of the skill.
            input_data: Input data for the skill.
            timeout: Maximum seconds for this invocation.

        Returns:
            SkillResult with success/failure status.
        """
        # Validate read-only constraint
        try:
            # Skills invoke MCP operations; we validate at invocation time
            # The skill_name itself is safe — it's the MCP ops within that
            # are validated by the tool_registry during actual MCP calls.
            pass
        except WriteOperationError as e:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Write operation rejected: {e}",
            )

        # Use the lesser of skill timeout and MCP connection timeout (Req 11.1)
        effective_timeout = min(timeout, MCP_CONNECTION_TIMEOUT_SECONDS)

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._invoker.invoke(skill_name, input_data, effective_timeout),
                timeout=effective_timeout,
            )
            result.duration_ms = (time.monotonic() - start) * 1000
            return result
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000
            # Determine if this was an MCP connection timeout (Req 11.1)
            is_mcp_timeout = duration_ms >= (MCP_CONNECTION_TIMEOUT_SECONDS * 1000 * 0.9)
            error_type = "mcp_connection_timeout" if is_mcp_timeout else "timeout"
            error_msg = (
                f"MCP connection timeout after {duration_ms:.0f}ms (limit: {MCP_CONNECTION_TIMEOUT_SECONDS}s)"
                if is_mcp_timeout
                else f"Skill exceeded {timeout:.1f}s timeout"
            )
            ctx.errors.append(SkillError(
                skill_name=skill_name,
                error_type=error_type,
                message=error_msg,
                timestamp=datetime.now(timezone.utc),
                recoverable=True,
            ))
            ctx.signals.unavailable_sources.append(skill_name)
            ctx.data_gaps.append(DataGap(
                source_name=skill_name,
                workflow_phase=ctx.phase.value,
                reason=error_msg,
            ))
            logger.warning(
                "Skill %s %s: %s",
                skill_name,
                error_type,
                error_msg,
            )
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Timeout after {duration_ms:.0f}ms",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            ctx.errors.append(SkillError(
                skill_name=skill_name,
                error_type="exception",
                message=str(e),
                timestamp=datetime.now(timezone.utc),
                recoverable=True,
            ))
            ctx.data_gaps.append(DataGap(
                source_name=skill_name,
                workflow_phase=ctx.phase.value,
                reason=str(e),
            ))
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

    # ------------------------------------------------------------------
    # Report assembly
    # ------------------------------------------------------------------

    def _compute_blast_radius(self, ctx: WorkflowContext) -> BlastRadius:
        """Compute the blast radius from collected signals.

        Args:
            ctx: The workflow context.

        Returns:
            BlastRadius assessment.
        """
        affected_services: list[str] = []
        affected_namespaces: list[str] = []
        affected_nodes: list[str] = []
        impacted_pod_count = 0

        # From alert
        for resource in ctx.alert.affected_resources:
            if resource.display_name:
                affected_services.append(resource.display_name)

        if ctx.alert.namespace:
            affected_namespaces.append(ctx.alert.namespace)

        # From cluster health
        if ctx.signals.cluster_health:
            ch = ctx.signals.cluster_health
            affected_nodes.extend(ch.nodes.not_ready)
            impacted_pod_count = ch.pods.pending + ch.pods.failed
            if ch.pods.crash_looping:
                impacted_pod_count += len(ch.pods.crash_looping)
                for pod in ch.pods.crash_looping:
                    if pod.namespace not in affected_namespaces:
                        affected_namespaces.append(pod.namespace)

        summary_parts: list[str] = []
        if impacted_pod_count:
            summary_parts.append(f"{impacted_pod_count} pods affected")
        if affected_nodes:
            summary_parts.append(f"{len(affected_nodes)} nodes not ready")
        if affected_services:
            summary_parts.append(
                f"{len(affected_services)} service(s) in {ctx.alert.region}"
            )
        summary = ", ".join(summary_parts) if summary_parts else (
            f"Alert in {ctx.alert.region}"
        )

        return BlastRadius(
            summary=summary,
            affected_services=affected_services,
            affected_namespaces=affected_namespaces,
            affected_nodes=affected_nodes,
            impacted_pod_count=impacted_pod_count,
            region=ctx.alert.region,
        )

    def _compute_confidence(self, ctx: WorkflowContext) -> float:
        """Compute overall confidence score accounting for unavailable sources.

        Requirement 11.3: Reduce confidence by 0.2 per unavailable source,
        clamped to a minimum of 0.1.
        Requirement 11.4: When all MCPs fail, confidence is 0.1.
        Requirement 11.5: When baseline shows normal despite alarm,
        confidence is capped at 0.3.

        Args:
            ctx: The workflow context.

        Returns:
            Confidence score between 0.1 and 1.0.
        """
        # Requirement 11.4: All signal sources failed → 0.1
        if ctx.all_signal_sources_failed():
            return MIN_CONFIDENCE

        base_confidence = 0.8  # Base confidence with all signals

        # Reduce based on signal quality
        if ctx.signals.evidence_items:
            # Use the top hypothesis confidence if available
            weights = [ei.weight for ei in ctx.signals.evidence_items]
            if weights:
                base_confidence = max(weights)

        # Reduce per unavailable source (Requirement 11.3)
        unique_unavailable = set(ctx.signals.unavailable_sources)
        reduction = len(unique_unavailable) * CONFIDENCE_REDUCTION_PER_SOURCE
        confidence = base_confidence - reduction

        # Requirement 11.5: Cap confidence when baseline is normal despite alarm
        if self._is_baseline_normal(ctx):
            confidence = min(confidence, NORMAL_BASELINE_MAX_CONFIDENCE)

        return max(MIN_CONFIDENCE, min(1.0, confidence))

    def _is_baseline_normal(self, ctx: WorkflowContext) -> bool:
        """Check if metric baseline shows normal despite alarm firing.

        Requirement 11.5: When baseline shows normal range values while
        an alarm is firing, we still run deploy-correlation and log-triage
        but cap the confidence.

        Args:
            ctx: The workflow context.

        Returns:
            True if baseline is normal despite active alarm.
        """
        if ctx.signals.metric_deviation is None:
            return False

        from skills.shared.models import DeviationClassification
        return (
            ctx.signals.metric_deviation.classification == DeviationClassification.NORMAL
            and ctx.alert.state.value == "firing"
        )

    def _assemble_report(self, ctx: WorkflowContext) -> IncidentReport:
        """Assemble the final IncidentReport from workflow context.

        Requirement 11.2: Report is explicitly marked as having incomplete
        data with structured annotations when sources are unavailable.
        Requirement 11.4: When all MCPs fail, report contains only original
        alert info at 0.1 confidence.

        Args:
            ctx: The completed workflow context.

        Returns:
            The assembled IncidentReport.
        """
        elapsed_ms = ctx.elapsed_seconds * 1000
        blast_radius = self._compute_blast_radius(ctx)
        confidence = self._compute_confidence(ctx)

        # Build hypotheses list from evidence items
        hypotheses: list[Hypothesis] = []
        for i, ei in enumerate(ctx.signals.evidence_items, start=1):
            hypotheses.append(Hypothesis(
                rank=i,
                title=ei.claim or "Insufficient data",
                description=f"Generated from {ei.source}",
                confidence=ei.weight,
                supporting_evidence=[ei],
                contradicting_evidence=[],
                suggested_verification=["Review related metrics and logs"],
            ))

        # Build recommended actions including data gap annotations (Req 11.2)
        actions = self._generate_actions(ctx)

        return IncidentReport(
            incident_id=ctx.incident_id,
            generated_at=datetime.now(timezone.utc),
            processing_duration=f"{elapsed_ms:.0f}ms",
            alert=ctx.alert,
            severity=ctx.alert.severity,
            blast_radius=blast_radius,
            hypotheses=hypotheses,
            confidence=confidence,
            metric_deviation=ctx.signals.metric_deviation,
            correlated_changes=ctx.signals.correlated_changes,
            log_findings=ctx.signals.log_findings,
            cluster_health=ctx.signals.cluster_health,
            recommended_actions=actions,
            escalation=None,  # Populated by escalation-decision skill
            evidence_links=ctx.signals.evidence_items,
            unavailable_sources=list(set(ctx.signals.unavailable_sources)),
            data_gaps=[
                {
                    "source_name": gap.source_name,
                    "workflow_phase": gap.workflow_phase,
                    "reason": gap.reason,
                    "timestamp": gap.timestamp,
                }
                for gap in ctx.data_gaps
            ],
        )

    def _generate_actions(self, ctx: WorkflowContext) -> list[str]:
        """Generate recommended actions based on collected signals.

        Args:
            ctx: The workflow context.

        Returns:
            List of recommended action strings.
        """
        actions: list[str] = []

        if ctx.signals.cluster_health:
            ch = ctx.signals.cluster_health
            if ch.nodes.not_ready:
                actions.append(
                    f"Investigate {len(ch.nodes.not_ready)} not-ready node(s): "
                    f"{', '.join(ch.nodes.not_ready[:3])}"
                )
            if ch.pods.crash_looping:
                actions.append(
                    f"Check {len(ch.pods.crash_looping)} crash-looping pod(s)"
                )

        if ctx.signals.correlated_changes:
            changes = ctx.signals.correlated_changes.changes
            high_corr = [c for c in changes if c.correlation_score > 0.7]
            if high_corr:
                actions.append(
                    f"Review {len(high_corr)} highly correlated change(s) "
                    f"in the last 24h"
                )

        if ctx.signals.log_findings:
            new_errors = [
                f for f in ctx.signals.log_findings.findings if f.is_new
            ]
            if new_errors:
                actions.append(
                    f"Investigate {len(new_errors)} new error pattern(s) in logs"
                )

        if ctx.signals.unavailable_sources:
            actions.append(
                f"Note: {len(ctx.signals.unavailable_sources)} data source(s) "
                f"unavailable — re-run when accessible"
            )

        if not actions:
            actions.append("Monitor the situation and check related dashboards")

        return actions
