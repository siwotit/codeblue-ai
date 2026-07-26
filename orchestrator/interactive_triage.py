"""Interactive CLI Triage Mode for CodeBlue AI.

Provides conversational incident investigation within Claude Code. Engineers
can start investigations with natural language, ask follow-up questions, and
invoke specific skills on demand.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 13.10
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from skills.shared.config import CodeBlueConfig, load_config
from skills.shared.models import (
    AlertSource,
    AlertState,
    ConversationTurn,
    EvidenceItem,
    Hypothesis,
    NormalizedAlert,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
    TriageResponse,
    TriageSession,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session Store (in-memory registry of active triage sessions)
# ---------------------------------------------------------------------------

_session_store: dict[str, TriageSession] = {}


def _store_session(session: TriageSession) -> None:
    """Register a session in the store."""
    _session_store[session.session_id] = session


def _get_session(session_id: str) -> TriageSession:
    """Retrieve a session by ID. Raises KeyError if not found."""
    if session_id not in _session_store:
        raise KeyError(f"Triage session not found: {session_id}")
    return _session_store[session_id]


def get_all_sessions() -> dict[str, TriageSession]:
    """Return a copy of the session store (for testing/debugging)."""
    return dict(_session_store)


def clear_sessions() -> None:
    """Clear all sessions (primarily for testing)."""
    _session_store.clear()


# ---------------------------------------------------------------------------
# Skill Registry (maps skill names to callable functions)
# ---------------------------------------------------------------------------

# Type alias for skill functions: they take params dict and return result dict
SkillFunction = Any  # Callable[[dict[str, Any]], dict[str, Any]]

_skill_registry: dict[str, SkillFunction] = {}


def register_skill(name: str, func: SkillFunction) -> None:
    """Register a skill function for direct invocation.

    Args:
        name: Skill name (e.g., "metric-baseline", "k8s-cluster-health").
        func: Callable that takes params dict and returns result dict.
    """
    _skill_registry[name] = func


def get_registered_skills() -> dict[str, SkillFunction]:
    """Return the current skill registry."""
    return dict(_skill_registry)


# ---------------------------------------------------------------------------
# Entity Extraction Patterns
# ---------------------------------------------------------------------------

# AWS regions pattern (e.g., us-east-1, eu-west-2, ap-southeast-1)
REGION_PATTERN = re.compile(
    r"\b(us|eu|ap|sa|ca|me|af)-(east|west|north|south|central|northeast|southeast|northwest|southwest)-\d\b"
)

# HTTP status codes (e.g., 500, 503, 404, 502)
HTTP_CODE_PATTERN = re.compile(r"\b([345]\d{2})\b")

# Kubernetes cluster names (common patterns like prod-cluster, staging-us-east-1, my-cluster-name)
CLUSTER_PATTERN = re.compile(
    r"\b([a-z][a-z0-9-]*(?:cluster|prod|staging|dev|eks)[a-z0-9-]*)\b"
    r"|\bcluster\s+([a-z][a-z0-9-]+)\b"
    r"|\b([a-z][a-z0-9-]*(?:-[a-z0-9]+)+)\b"
)

# Service names (common patterns: service-name, svc-name, or known keywords)
SERVICE_KEYWORDS = {
    "api",
    "gateway",
    "auth",
    "payment",
    "payments",
    "checkout",
    "order",
    "orders",
    "inventory",
    "shipping",
    "notification",
    "notifications",
    "user",
    "users",
    "search",
    "catalog",
    "cart",
    "frontend",
    "backend",
    "worker",
    "scheduler",
    "ingress",
    "proxy",
    "cache",
    "queue",
    "database",
    "db",
    "redis",
    "kafka",
    "nginx",
}

SERVICE_PATTERN = re.compile(
    r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:-(?:service|svc|api|worker|server)))\b"
)

# Namespace patterns (e.g., "in the payments namespace", "namespace kube-system")
NAMESPACE_PATTERN = re.compile(
    r"namespace[:\s]+([a-z][a-z0-9-]*)"
    r"|\bin(?:to)?\s+(?:the\s+)?([a-z][a-z0-9-]*)\s+namespace\b"
)

# Severity indicators from natural language
SEVERITY_KEYWORDS: dict[Severity, list[str]] = {
    Severity.CRITICAL: [
        "critical",
        "down",
        "outage",
        "completely",
        "total failure",
        "emergency",
        "p0",
        "sev1",
        "sev-1",
    ],
    Severity.HIGH: [
        "high",
        "severe",
        "major",
        "significant",
        "p1",
        "sev2",
        "sev-2",
        "intermittent failures",
    ],
    Severity.MEDIUM: [
        "medium",
        "moderate",
        "elevated",
        "increased",
        "p2",
        "sev3",
        "sev-3",
    ],
    Severity.LOW: [
        "low",
        "minor",
        "degraded",
        "slight",
        "p3",
        "sev4",
        "sev-4",
    ],
}


# ---------------------------------------------------------------------------
# Field Annotation (inferred vs explicit)
# ---------------------------------------------------------------------------


@dataclass
class FieldAnnotation:
    """Tracks whether a field was explicitly stated or inferred."""

    field_name: str
    value: Any
    source: str  # "explicit" or "inferred"
    reason: str = ""  # Explanation of why it was inferred

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "field_name": self.field_name,
            "value": str(self.value),
            "source": self.source,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Entity Extraction
# ---------------------------------------------------------------------------


@dataclass
class ExtractedEntities:
    """Entities extracted from a natural language description."""

    clusters: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    http_codes: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    severity: Severity | None = None
    raw_description: str = ""


def extract_entities(description: str) -> ExtractedEntities:
    """Extract structured entities from a natural language incident description.

    Uses regex patterns and keyword matching to identify:
    - AWS regions (us-east-1, eu-west-2, etc.)
    - HTTP status codes (500, 503, etc.)
    - Cluster names (prod-cluster, my-eks-cluster, etc.)
    - Service names (payment-service, auth-api, etc.)
    - Namespaces (payments, kube-system, etc.)
    - Severity indicators (critical, down, outage, etc.)

    Args:
        description: Natural language description of the incident.

    Returns:
        ExtractedEntities with all identified entities.
    """
    entities = ExtractedEntities(raw_description=description)
    lower_desc = description.lower()

    # Extract regions
    entities.regions = list(set(REGION_PATTERN.findall(description.lower())))
    # The pattern captures groups - reconstruct full region strings
    region_matches = REGION_PATTERN.finditer(description.lower())
    entities.regions = list(set(m.group(0) for m in region_matches))

    # Extract HTTP codes
    http_matches = HTTP_CODE_PATTERN.finditer(description)
    entities.http_codes = list(set(m.group(1) for m in http_matches))

    # Extract namespaces (must come before cluster extraction)
    ns_matches = NAMESPACE_PATTERN.finditer(lower_desc)
    for m in ns_matches:
        ns = m.group(1) or m.group(2)
        if ns and ns not in entities.namespaces:
            entities.namespaces.append(ns)

    # Extract cluster names
    entities.clusters = _extract_clusters(lower_desc)

    # Extract service names
    entities.services = _extract_services(lower_desc)

    # Extract severity
    entities.severity = _extract_severity(lower_desc)

    return entities


def _extract_clusters(description: str) -> list[str]:
    """Extract cluster names from description.

    Looks for patterns like "cluster X", "prod-cluster", "my-eks-cluster",
    or any hyphenated name that looks like a cluster identifier.

    Args:
        description: Lowercased description text.

    Returns:
        List of extracted cluster names.
    """
    clusters: list[str] = []

    # Look for explicit "cluster <name>" patterns
    explicit_pattern = re.compile(r"cluster\s+([a-z][a-z0-9](?:[a-z0-9-]*[a-z0-9])?)")
    for m in explicit_pattern.finditer(description):
        name = m.group(1)
        if name not in clusters and len(name) > 2:
            clusters.append(name)

    # Look for names containing cluster-related keywords
    cluster_keyword_pattern = re.compile(
        r"\b([a-z][a-z0-9-]*(?:cluster|eks)[a-z0-9-]*)\b"
    )
    for m in cluster_keyword_pattern.finditer(description):
        name = m.group(1)
        if name not in clusters and len(name) > 3:
            clusters.append(name)

    # Look for "prod-<region>" or "<env>-<name>" patterns commonly used for clusters
    env_pattern = re.compile(
        r"\b(prod|staging|dev|test)[-_]([a-z][a-z0-9-]+)\b"
    )
    for m in env_pattern.finditer(description):
        name = f"{m.group(1)}-{m.group(2)}"
        if name not in clusters and len(name) > 4:
            clusters.append(name)

    return clusters


def _extract_services(description: str) -> list[str]:
    """Extract service names from description.

    Args:
        description: Lowercased description text.

    Returns:
        List of extracted service names.
    """
    services: list[str] = []

    # Match explicit service patterns (e.g., "payment-service", "auth-api")
    svc_matches = SERVICE_PATTERN.finditer(description)
    for m in svc_matches:
        name = m.group(1)
        if name not in services:
            services.append(name)

    # Match known service keywords in context
    words = re.findall(r"\b[a-z][a-z0-9-]*\b", description)
    for word in words:
        if word in SERVICE_KEYWORDS and word not in services:
            services.append(word)

    return services


def _extract_severity(description: str) -> Severity | None:
    """Extract severity from natural language indicators.

    Checks for keywords in order of severity (critical → low).
    Returns the highest severity found, or None if no indicators present.

    Args:
        description: Lowercased description text.

    Returns:
        Extracted Severity or None.
    """
    for severity, keywords in SEVERITY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in description:
                return severity
    return None


# ---------------------------------------------------------------------------
# NormalizedAlert Synthesis
# ---------------------------------------------------------------------------


def synthesize_alert(
    entities: ExtractedEntities,
    config: CodeBlueConfig,
) -> tuple[NormalizedAlert, list[FieldAnnotation]]:
    """Synthesize a NormalizedAlert from extracted entities and defaults.

    Populates the alert fields from extracted entities where available,
    and assigns reasonable defaults for missing fields. Tracks which fields
    were explicitly stated vs inferred.

    Args:
        entities: Entities extracted from the user's description.
        config: Application configuration for defaults.

    Returns:
        Tuple of (NormalizedAlert, list of FieldAnnotations).
    """
    annotations: list[FieldAnnotation] = []

    # --- Severity ---
    if entities.severity is not None:
        severity = entities.severity
        annotations.append(FieldAnnotation(
            field_name="severity",
            value=severity.value,
            source="explicit",
            reason="Severity indicator found in description",
        ))
    else:
        severity = Severity.MEDIUM
        annotations.append(FieldAnnotation(
            field_name="severity",
            value=severity.value,
            source="inferred",
            reason="No severity indicator found; defaulting to medium",
        ))

    # --- Region ---
    if entities.regions:
        region = entities.regions[0]
        annotations.append(FieldAnnotation(
            field_name="region",
            value=region,
            source="explicit",
            reason="Region found in description",
        ))
    else:
        region = config.region
        annotations.append(FieldAnnotation(
            field_name="region",
            value=region,
            source="inferred",
            reason=f"No region found; using configured default ({config.region})",
        ))

    # --- Title ---
    title = _generate_title(entities)
    annotations.append(FieldAnnotation(
        field_name="title",
        value=title,
        source="explicit",
        reason="Generated from user description",
    ))

    # --- Cluster ---
    cluster: str | None = None
    if entities.clusters:
        cluster = entities.clusters[0]
        annotations.append(FieldAnnotation(
            field_name="cluster",
            value=cluster,
            source="explicit",
            reason="Cluster name found in description",
        ))

    # --- Namespace ---
    namespace: str | None = None
    if entities.namespaces:
        namespace = entities.namespaces[0]
        annotations.append(FieldAnnotation(
            field_name="namespace",
            value=namespace,
            source="explicit",
            reason="Namespace found in description",
        ))

    # --- Affected Resources ---
    affected_resources = _build_affected_resources(entities)
    if affected_resources:
        annotations.append(FieldAnnotation(
            field_name="affectedResources",
            value=f"{len(affected_resources)} resource(s)",
            source="explicit",
            reason="Resources inferred from entities in description",
        ))
    else:
        annotations.append(FieldAnnotation(
            field_name="affectedResources",
            value="0 resources",
            source="inferred",
            reason="No specific resources identified in description",
        ))

    # --- Source ---
    # If cluster is mentioned, likely K8s-originated; otherwise assume CloudWatch
    if cluster:
        source = AlertSource.KUBERNETES
        annotations.append(FieldAnnotation(
            field_name="source",
            value=source.value,
            source="inferred",
            reason="Cluster name present; assuming Kubernetes source",
        ))
    else:
        source = AlertSource.CLOUDWATCH
        annotations.append(FieldAnnotation(
            field_name="source",
            value=source.value,
            source="inferred",
            reason="No cluster context; defaulting to CloudWatch source",
        ))

    alert = NormalizedAlert(
        id=f"triage-{uuid.uuid4().hex[:12]}",
        source=source,
        source_alarm_id="interactive-triage",
        severity=severity,
        title=title,
        description=entities.raw_description,
        state=AlertState.FIRING,
        fired_at=datetime.now(timezone.utc),
        affected_resources=affected_resources,
        region=region,
        cluster=cluster,
        namespace=namespace,
        raw_payload={
            "interactive_triage": True,
            "user_description": entities.raw_description,
            "extracted_entities": {
                "clusters": entities.clusters,
                "regions": entities.regions,
                "http_codes": entities.http_codes,
                "services": entities.services,
                "namespaces": entities.namespaces,
            },
        },
    )

    return alert, annotations


def _generate_title(entities: ExtractedEntities) -> str:
    """Generate a concise title from extracted entities.

    Args:
        entities: Extracted entities.

    Returns:
        Human-readable alert title.
    """
    parts: list[str] = []

    if entities.http_codes:
        codes = ", ".join(sorted(entities.http_codes)[:3])
        parts.append(f"HTTP {codes}")

    if entities.services:
        svc = entities.services[0]
        parts.append(f"in {svc}")

    if entities.clusters:
        parts.append(f"on {entities.clusters[0]}")

    if parts:
        return " ".join(parts)

    # Fallback: use first 80 chars of description
    desc = entities.raw_description.strip()
    if len(desc) > 80:
        return desc[:77] + "..."
    return desc or "Interactive triage investigation"


def _build_affected_resources(
    entities: ExtractedEntities,
) -> list[ResourceIdentifier]:
    """Build resource identifiers from extracted entities.

    Args:
        entities: Extracted entities.

    Returns:
        List of ResourceIdentifier objects.
    """
    resources: list[ResourceIdentifier] = []

    # Add cluster as K8s resource
    for cluster in entities.clusters:
        resources.append(ResourceIdentifier(
            type=ResourceIdentifierType.K8S_RESOURCE,
            value=cluster,
            displayName=f"cluster/{cluster}",
        ))

    # Add services as resources
    for service in entities.services:
        resources.append(ResourceIdentifier(
            type=ResourceIdentifierType.K8S_RESOURCE,
            value=service,
            displayName=service,
        ))

    return resources


# ---------------------------------------------------------------------------
# Triage Session Management
# ---------------------------------------------------------------------------


def _determine_initial_skills(alert: NormalizedAlert) -> list[str]:
    """Determine which signal-collection skills to invoke initially.

    Based on the synthesized alert context, picks at least one skill
    to start the investigation.

    Args:
        alert: The synthesized NormalizedAlert.

    Returns:
        List of skill names to invoke.
    """
    skills: list[str] = []

    # Always run metric-baseline and log-triage for any investigation
    skills.append("metric-baseline")
    skills.append("log-triage")

    # If a cluster is specified, also run k8s-cluster-health
    if alert.cluster:
        skills.append("k8s-cluster-health")

    return skills


def start_investigation(
    prompt: str,
    config: CodeBlueConfig | None = None,
) -> tuple[TriageSession, list[FieldAnnotation], list[str]]:
    """Start a new interactive triage investigation from natural language.

    This is the primary entry point for interactive triage mode. It:
    1. Extracts entities from the user's natural language description
    2. Synthesizes a NormalizedAlert with defaults for missing fields
    3. Annotates which fields were inferred vs explicitly stated
    4. Creates a TriageSession with unique ID, empty accumulators
    5. Determines which initial signal-collection skills to invoke

    Args:
        prompt: Natural language description of the incident.
        config: Optional configuration (loaded from defaults if not provided).

    Returns:
        Tuple of (TriageSession, field_annotations, skills_to_invoke).
        The caller is responsible for actually invoking the skills.

    Requirements: 13.1, 13.2, 13.3
    """
    if config is None:
        config = load_config()

    # Step 1: Extract entities from user description
    entities = extract_entities(prompt)

    # Step 2: Synthesize NormalizedAlert from entities + defaults
    alert, annotations = synthesize_alert(entities, config)

    # Step 3: Determine initial skills to invoke
    skills_to_invoke = _determine_initial_skills(alert)

    # Step 4: Create the triage session
    session = TriageSession(
        session_id=str(uuid.uuid4()),
        started_at=datetime.now(timezone.utc),
        normalized_alert=alert,
        evidence_accumulator=[],
        conversation_history=[
            ConversationTurn(
                role="user",
                content=prompt,
                timestamp=datetime.now(timezone.utc),
                skills_invoked=[],
                evidence_added=[],
            )
        ],
        active_hypotheses=[],
    )

    logger.info(
        "Started triage session %s with %d initial skills: %s",
        session.session_id,
        len(skills_to_invoke),
        skills_to_invoke,
    )

    # Store session for follow-up access
    _store_session(session)

    return session, annotations, skills_to_invoke


# ---------------------------------------------------------------------------
# Follow-up and Evidence Accumulation (Requirements: 13.4–13.10)
# ---------------------------------------------------------------------------


def _determine_follow_up_skills(
    prompt: str,
    entities: ExtractedEntities,
    session: TriageSession,
) -> list[str]:
    """Determine which skills to invoke for a follow-up prompt.

    Uses extracted entities and prompt content to decide skills.

    Args:
        prompt: The follow-up prompt text.
        entities: Entities extracted from the follow-up.
        session: The current triage session.

    Returns:
        List of skill names to invoke.
    """
    skills: list[str] = []
    lower_prompt = prompt.lower()

    # Keywords that map to specific skills
    skill_keywords: dict[str, list[str]] = {
        "metric-baseline": ["metric", "metrics", "baseline", "deviation", "anomaly", "cpu", "memory", "latency"],
        "log-triage": ["log", "logs", "error", "exception", "timeout", "fatal"],
        "k8s-cluster-health": ["cluster", "node", "health", "kubernetes", "k8s"],
        "pod-failure-triage": ["pod", "pods", "crash", "oom", "crashloop", "failure", "failures"],
        "node-condition-check": ["node", "nodes", "condition", "pressure", "disk", "memory pressure"],
        "eks-addon-status": ["addon", "addons", "cni", "coredns", "kube-proxy", "ebs"],
        "deploy-correlation": ["deploy", "deployment", "rollout", "change", "changes", "cloudtrail"],
        "hypothesis-engine": ["hypothesis", "hypotheses", "guess", "root cause", "diagnosis", "what happened"],
    }

    for skill_name, keywords in skill_keywords.items():
        for keyword in keywords:
            if keyword in lower_prompt:
                if skill_name not in skills:
                    skills.append(skill_name)
                break

    # If entities reference a cluster but no k8s skill matched, add cluster health
    if entities.clusters and not any(s.startswith("k8s") or s.startswith("pod") or s.startswith("node") or s.startswith("eks") for s in skills):
        skills.append("k8s-cluster-health")

    # Default: if nothing matched, run log-triage as a general diagnostic
    if not skills:
        skills.append("log-triage")

    return skills


def _update_session_scope(
    session: TriageSession,
    entities: ExtractedEntities,
) -> None:
    """Update session's NormalizedAlert scope based on new entities.

    Narrows the scope when the follow-up references a specific namespace,
    cluster, or resource not in the original alert (Requirement 13.9).

    Args:
        session: The triage session to update.
        entities: Newly extracted entities from the follow-up.
    """
    alert = session.normalized_alert

    # Narrow to specific namespace if mentioned
    if entities.namespaces:
        alert.namespace = entities.namespaces[0]

    # Narrow to specific cluster if mentioned
    if entities.clusters:
        alert.cluster = entities.clusters[0]

    # Add new services to affected resources
    for service in entities.services:
        existing_values = {r.value for r in alert.affected_resources}
        if service not in existing_values:
            alert.affected_resources.append(
                ResourceIdentifier(
                    type=ResourceIdentifierType.K8S_RESOURCE,
                    value=service,
                    displayName=service,
                )
            )


def _execute_skill(
    skill_name: str,
    params: dict[str, Any],
    session: TriageSession,
) -> tuple[list[EvidenceItem], str | None]:
    """Execute a single skill and return evidence items.

    Handles skill failures gracefully (Requirement 13.10): on failure,
    returns empty evidence and an error message instead of raising.

    Args:
        skill_name: Name of the skill to invoke.
        params: Parameters to pass to the skill.
        session: The current session (for context).

    Returns:
        Tuple of (evidence_items, error_message_or_none).
    """
    if skill_name not in _skill_registry:
        # Skill not registered — produce a synthetic evidence item noting the gap
        logger.warning("Skill %s not registered, skipping", skill_name)
        return [], f"Skill '{skill_name}' is not available"

    try:
        skill_func = _skill_registry[skill_name]
        result = skill_func(params)

        # Convert result to evidence items
        evidence_items: list[EvidenceItem] = []
        if isinstance(result, dict):
            # If the skill returns evidence items directly
            if "evidence_items" in result:
                for item_data in result["evidence_items"]:
                    if isinstance(item_data, EvidenceItem):
                        evidence_items.append(item_data)
                    elif isinstance(item_data, dict):
                        evidence_items.append(EvidenceItem(
                            claim=item_data.get("claim", ""),
                            source=item_data.get("source", skill_name),
                            timestamp=item_data.get("timestamp"),
                            weight=item_data.get("weight", 0.5),
                        ))
            else:
                # Wrap the entire result as a single evidence item
                summary = result.get("summary", result.get("description", f"{skill_name} result"))
                evidence_items.append(EvidenceItem(
                    claim=str(summary),
                    source=skill_name,
                    timestamp=datetime.now(timezone.utc),
                    weight=0.5,
                ))

        return evidence_items, None

    except Exception as e:
        error_msg = f"Skill '{skill_name}' failed: {type(e).__name__}: {e}"
        logger.error(error_msg, exc_info=True)
        return [], error_msg


def follow_up(
    session_id: str,
    prompt: str,
) -> TriageResponse:
    """Submit a follow-up question to an existing triage session.

    Appends the follow-up as a new ConversationTurn, extracts entities
    for scope narrowing, determines relevant skills, executes them,
    and returns a TriageResponse with accumulated evidence.

    Evidence only accumulates — previously collected signals are never removed.

    Args:
        session_id: ID of the existing triage session.
        prompt: The follow-up question or instruction.

    Returns:
        TriageResponse with summary, new evidence, updated hypotheses,
        and suggested next steps.

    Raises:
        KeyError: If session_id is not found.

    Requirements: 13.4, 13.6, 13.7, 13.8, 13.9, 13.10
    """
    session = _get_session(session_id)

    # Step 1: Extract entities from follow-up for scope narrowing
    entities = extract_entities(prompt)

    # Step 2: Update session scope based on new entities (Req 13.9)
    _update_session_scope(session, entities)

    # Step 3: Determine which skills to invoke
    skills_to_invoke = _determine_follow_up_skills(prompt, entities, session)

    # Step 4: Append user turn to conversation history
    user_turn = ConversationTurn(
        role="user",
        content=prompt,
        timestamp=datetime.now(timezone.utc),
        skills_invoked=[],
        evidence_added=[],
    )
    session.conversation_history.append(user_turn)

    # Step 5: Execute skills and accumulate evidence
    new_evidence: list[EvidenceItem] = []
    skills_executed: list[str] = []
    errors: list[str] = []

    for skill_name in skills_to_invoke:
        params = _build_skill_params(skill_name, session, entities)
        evidence_items, error = _execute_skill(skill_name, params, session)

        if error:
            errors.append(error)
        else:
            skills_executed.append(skill_name)

        # Accumulate evidence (Req 13.6, 13.7) — always append, never discard
        new_evidence.extend(evidence_items)
        session.evidence_accumulator.extend(evidence_items)

    # Step 6: Build suggested next steps
    suggested_steps = _build_suggested_next_steps(session, skills_executed, errors)

    # Step 7: Build summary
    summary = _build_follow_up_summary(prompt, skills_executed, new_evidence, errors)

    # Step 8: Append agent response turn
    agent_turn = ConversationTurn(
        role="agent",
        content=summary,
        timestamp=datetime.now(timezone.utc),
        skills_invoked=skills_executed,
        evidence_added=new_evidence,
    )
    session.conversation_history.append(agent_turn)

    # Step 9: Update session in store
    _store_session(session)

    logger.info(
        "Follow-up on session %s: invoked %d skills, added %d evidence items",
        session_id,
        len(skills_executed),
        len(new_evidence),
    )

    return TriageResponse(
        summary=summary,
        new_evidence=new_evidence,
        updated_hypotheses=session.active_hypotheses,
        suggested_next_steps=suggested_steps,
    )


def invoke_skill(
    session_id: str,
    skill_name: str,
    params: dict[str, Any] | None = None,
) -> TriageResponse:
    """Directly invoke a specific skill within a triage session.

    Bypasses the orchestrator's standard workflow sequencing (Requirement 13.5).
    Results are appended to the session's evidence accumulator.
    Skill failures are handled gracefully (Requirement 13.10).

    Args:
        session_id: ID of the existing triage session.
        skill_name: Name of the skill to invoke.
        params: Optional parameters to pass to the skill.

    Returns:
        TriageResponse with the skill's results.

    Raises:
        KeyError: If session_id is not found.

    Requirements: 13.5, 13.6, 13.7, 13.8, 13.10
    """
    session = _get_session(session_id)
    if params is None:
        params = {}

    # Append user turn for the skill invocation request
    user_turn = ConversationTurn(
        role="user",
        content=f"Invoke skill: {skill_name}",
        timestamp=datetime.now(timezone.utc),
        skills_invoked=[],
        evidence_added=[],
    )
    session.conversation_history.append(user_turn)

    # Execute the skill
    evidence_items, error = _execute_skill(skill_name, params, session)

    # Accumulate evidence — always append, never remove (Req 13.6, 13.7)
    session.evidence_accumulator.extend(evidence_items)

    # Build response
    skills_executed = [skill_name] if not error else []
    errors = [error] if error else []

    summary = _build_skill_invocation_summary(skill_name, evidence_items, error)
    suggested_steps = _build_suggested_next_steps(session, skills_executed, errors)

    # Append agent response turn
    agent_turn = ConversationTurn(
        role="agent",
        content=summary,
        timestamp=datetime.now(timezone.utc),
        skills_invoked=skills_executed,
        evidence_added=evidence_items,
    )
    session.conversation_history.append(agent_turn)

    # Update session in store
    _store_session(session)

    logger.info(
        "Skill invocation on session %s: skill=%s, evidence_items=%d, error=%s",
        session_id,
        skill_name,
        len(evidence_items),
        error,
    )

    return TriageResponse(
        summary=summary,
        new_evidence=evidence_items,
        updated_hypotheses=session.active_hypotheses,
        suggested_next_steps=suggested_steps,
    )


# ---------------------------------------------------------------------------
# Helper functions for building responses
# ---------------------------------------------------------------------------


def _build_skill_params(
    skill_name: str,
    session: TriageSession,
    entities: ExtractedEntities,
) -> dict[str, Any]:
    """Build parameters for a skill invocation based on session context.

    Args:
        skill_name: The skill to build params for.
        session: Current triage session.
        entities: Extracted entities from the current prompt.

    Returns:
        Parameters dict for the skill.
    """
    alert = session.normalized_alert
    params: dict[str, Any] = {
        "alert": alert.model_dump(),
        "session_id": session.session_id,
    }

    if alert.cluster:
        params["cluster"] = alert.cluster
    if alert.namespace:
        params["namespace"] = alert.namespace
    if alert.region:
        params["region"] = alert.region
    if entities.services:
        params["services"] = entities.services
    if entities.namespaces:
        params["namespaces"] = entities.namespaces

    return params


def _build_follow_up_summary(
    prompt: str,
    skills_executed: list[str],
    new_evidence: list[EvidenceItem],
    errors: list[str],
) -> str:
    """Build a human-readable summary for a follow-up response.

    Args:
        prompt: The original follow-up prompt.
        skills_executed: Skills that ran successfully.
        new_evidence: New evidence items collected.
        errors: Any error messages from failed skills.

    Returns:
        Summary string.
    """
    parts: list[str] = []

    if skills_executed:
        parts.append(f"Invoked {len(skills_executed)} skill(s): {', '.join(skills_executed)}.")
    if new_evidence:
        parts.append(f"Collected {len(new_evidence)} new evidence item(s).")
    if errors:
        parts.append(f"Encountered {len(errors)} error(s): {'; '.join(errors)}.")
    if not parts:
        parts.append("No additional findings from this query.")

    return " ".join(parts)


def _build_skill_invocation_summary(
    skill_name: str,
    evidence_items: list[EvidenceItem],
    error: str | None,
) -> str:
    """Build a summary for a direct skill invocation.

    Args:
        skill_name: Name of the invoked skill.
        evidence_items: Evidence items returned.
        error: Error message if the skill failed.

    Returns:
        Summary string.
    """
    if error:
        return f"Skill '{skill_name}' failed: {error}. Session continues with previously collected evidence."
    if evidence_items:
        return f"Skill '{skill_name}' returned {len(evidence_items)} evidence item(s)."
    return f"Skill '{skill_name}' completed with no new evidence."


def _build_suggested_next_steps(
    session: TriageSession,
    skills_executed: list[str],
    errors: list[str],
) -> list[str]:
    """Build suggested next steps based on session state.

    Args:
        session: Current triage session.
        skills_executed: Skills that were just executed.
        errors: Any errors encountered.

    Returns:
        List of suggested next steps.
    """
    steps: list[str] = []

    # Suggest hypothesis generation if enough evidence is collected
    evidence_count = len(session.evidence_accumulator)
    if evidence_count >= 3 and "hypothesis-engine" not in skills_executed:
        steps.append("Run hypothesis generation to analyze accumulated evidence")

    # Suggest related skills that haven't been run yet
    all_skills_invoked = set()
    for turn in session.conversation_history:
        all_skills_invoked.update(turn.skills_invoked)

    alert = session.normalized_alert
    if alert.cluster and "k8s-cluster-health" not in all_skills_invoked:
        steps.append(f"Check cluster health for {alert.cluster}")
    if "deploy-correlation" not in all_skills_invoked:
        steps.append("Check for recent deployments or changes")
    if "log-triage" not in all_skills_invoked:
        steps.append("Search logs for error patterns")

    # If errors occurred, suggest retrying or alternative
    if errors:
        steps.append("Retry failed skill invocations or try alternative diagnostic approaches")

    # Always suggest a next action
    if not steps:
        steps.append("Ask a follow-up question to dig deeper into specific findings")

    return steps
