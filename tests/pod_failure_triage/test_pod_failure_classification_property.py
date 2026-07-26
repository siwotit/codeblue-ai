"""Property-based tests for pod failure classification correctness (Property 9).

**Validates: Requirement 3.4**

Property 9: Pod failure classification correctness
- For any pod with a failure state, verify it is classified into exactly one valid reason.
- For any pod that IS classified (result is not None), verify:
  1. The reason is one of the 5 valid PodFailureReason values
  2. The namespace is always populated
  3. The name is always populated
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# Add project root to path for imports
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, PROJECT_ROOT)

# Add the pod-failure-triage skill directory (hyphenated, not importable as package)
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "skills" / "pod-failure-triage"),
)

from pod_failure_triage import classify_pod_failure
from skills.shared.models import PodFailureReason


# --- Valid failure reasons ---
VALID_REASONS = {
    PodFailureReason.CRASH_LOOP_BACK_OFF,
    PodFailureReason.OOM_KILLED,
    PodFailureReason.IMAGE_PULL_BACK_OFF,
    PodFailureReason.CREATE_CONTAINER_ERROR,
    PodFailureReason.SANDBOX_ERROR,
}

VALID_REASON_VALUES = {
    "CrashLoopBackOff",
    "OOMKilled",
    "ImagePullBackOff",
    "CreateContainerError",
    "SandboxError",
}

# --- Strategies for generating pod data with failure states ---

safe_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=50,
)

namespace_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1,
    max_size=30,
)

pod_name_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1,
    max_size=50,
)

timestamp_text = st.sampled_from([
    "2024-01-15T10:30:00Z",
    "2024-06-01T08:00:00Z",
    "2024-12-25T23:59:59Z",
    "2024-03-10T14:22:33Z",
])

waiting_reasons = st.sampled_from([
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerError",
])


@st.composite
def pod_metadata(draw):
    """Generate pod metadata with name and namespace always populated."""
    return {
        "name": draw(pod_name_text),
        "namespace": draw(namespace_text),
    }


@st.composite
def crashed_container_status(draw):
    """Generate a container status in CrashLoopBackOff waiting state."""
    return {
        "name": draw(safe_text),
        "state": {
            "waiting": {
                "reason": "CrashLoopBackOff",
                "message": draw(st.one_of(st.none(), safe_text)) or "Back-off restarting",
            }
        },
        "lastState": {},
        "restartCount": draw(st.integers(min_value=1, max_value=100)),
    }


@st.composite
def oom_killed_container_status(draw):
    """Generate a container status that was OOMKilled."""
    use_exit_code = draw(st.booleans())
    terminated = {
        "finishedAt": draw(timestamp_text),
        "message": "OOMKilled",
    }
    if use_exit_code:
        terminated["exitCode"] = 137
        terminated["reason"] = "OOMKilled"
    else:
        terminated["reason"] = "OOMKilled"
        terminated["exitCode"] = 137
    return {
        "name": draw(safe_text),
        "state": {"terminated": terminated},
        "lastState": {},
        "restartCount": draw(st.integers(min_value=0, max_value=50)),
    }


@st.composite
def image_pull_container_status(draw):
    """Generate a container status with ImagePullBackOff."""
    reason = draw(st.sampled_from(["ImagePullBackOff", "ErrImagePull"]))
    return {
        "name": draw(safe_text),
        "state": {
            "waiting": {
                "reason": reason,
                "message": f"Failed to pull image: {draw(safe_text)}",
            }
        },
        "lastState": {},
        "restartCount": 0,
    }


@st.composite
def create_container_error_status(draw):
    """Generate a container status with CreateContainerError."""
    return {
        "name": draw(safe_text),
        "state": {
            "waiting": {
                "reason": "CreateContainerError",
                "message": draw(st.one_of(st.none(), safe_text)) or "Error creating container",
            }
        },
        "lastState": {},
        "restartCount": 0,
    }


@st.composite
def sandbox_error_conditions(draw):
    """Generate pod conditions indicating a sandbox error."""
    return [
        {
            "type": "Initialized",
            "status": "False",
            "reason": "SandboxCreationFailed",
            "message": draw(st.one_of(safe_text, st.just("Failed to create sandbox"))),
            "lastTransitionTime": draw(timestamp_text),
        }
    ]


@st.composite
def pod_with_crashloop(draw):
    """Generate a pod in CrashLoopBackOff state."""
    return {
        "metadata": draw(pod_metadata()),
        "status": {
            "phase": draw(st.sampled_from(["Pending", "Failed"])),
            "containerStatuses": [draw(crashed_container_status())],
            "initContainerStatuses": [],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": draw(timestamp_text),
                }
            ],
        },
    }


@st.composite
def pod_with_oom(draw):
    """Generate a pod with OOMKilled container."""
    return {
        "metadata": draw(pod_metadata()),
        "status": {
            "phase": draw(st.sampled_from(["Failed", "Pending"])),
            "containerStatuses": [draw(oom_killed_container_status())],
            "initContainerStatuses": [],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": draw(timestamp_text),
                }
            ],
        },
    }


@st.composite
def pod_with_image_pull_error(draw):
    """Generate a pod with ImagePullBackOff."""
    return {
        "metadata": draw(pod_metadata()),
        "status": {
            "phase": "Pending",
            "containerStatuses": [draw(image_pull_container_status())],
            "initContainerStatuses": [],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": draw(timestamp_text),
                }
            ],
        },
    }


@st.composite
def pod_with_create_container_error(draw):
    """Generate a pod with CreateContainerError."""
    return {
        "metadata": draw(pod_metadata()),
        "status": {
            "phase": "Pending",
            "containerStatuses": [draw(create_container_error_status())],
            "initContainerStatuses": [],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": draw(timestamp_text),
                }
            ],
        },
    }


@st.composite
def pod_with_sandbox_error(draw):
    """Generate a pod with SandboxError."""
    return {
        "metadata": draw(pod_metadata()),
        "status": {
            "phase": "Pending",
            "containerStatuses": [],
            "initContainerStatuses": [],
            "conditions": draw(sandbox_error_conditions()),
            "message": "",
        },
    }


@st.composite
def pod_with_sandbox_error_message(draw):
    """Generate a pod with sandbox error in status message."""
    return {
        "metadata": draw(pod_metadata()),
        "status": {
            "phase": "Pending",
            "containerStatuses": [],
            "initContainerStatuses": [],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": draw(timestamp_text),
                }
            ],
            "message": "failed to create sandbox for pod",
        },
    }


# Combined strategy for any pod with a failure state
any_failed_pod = st.one_of(
    pod_with_crashloop(),
    pod_with_oom(),
    pod_with_image_pull_error(),
    pod_with_create_container_error(),
    pod_with_sandbox_error(),
    pod_with_sandbox_error_message(),
)


# --- Property Tests ---


class TestPodFailureClassificationCorrectness:
    """Property 9: Pod failure classification correctness.

    **Validates: Requirement 3.4**

    For any pod with a failure state, verify it is classified into exactly one
    valid reason with populated name and namespace.
    """

    @given(pod=any_failed_pod)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_failed_pod_classified_into_valid_reason(self, pod):
        """Any pod with a known failure state is classified with a valid reason."""
        result = classify_pod_failure(pod)

        # Pod with a failure state must be classified
        assert result is not None, (
            f"Pod with failure state was not classified: {pod}"
        )

        # Must have a reason attribute with a valid PodFailureReason value
        assert hasattr(result, "reason"), "Classified result must have a 'reason' attribute"

        # Reason must be exactly one of the 5 valid values
        assert result.reason.value in VALID_REASON_VALUES, (
            f"Got invalid reason: {result.reason}. "
            f"Valid reasons are: {VALID_REASON_VALUES}"
        )

    @given(pod=any_failed_pod)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_classified_pod_has_populated_namespace(self, pod):
        """Any classified pod has a non-empty namespace."""
        result = classify_pod_failure(pod)

        if result is not None:
            assert result.namespace is not None, "Namespace should not be None"
            assert len(result.namespace) > 0, "Namespace should not be empty"

    @given(pod=any_failed_pod)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_classified_pod_has_populated_name(self, pod):
        """Any classified pod has a non-empty name."""
        result = classify_pod_failure(pod)

        if result is not None:
            assert result.name is not None, "Name should not be None"
            assert len(result.name) > 0, "Name should not be empty"

    @given(pod=pod_with_crashloop())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_crashloop_pod_classified_as_crashloop(self, pod):
        """Pods in CrashLoopBackOff state are classified as CRASH_LOOP_BACK_OFF."""
        result = classify_pod_failure(pod)
        assert result is not None
        assert result.reason.value == "CrashLoopBackOff"

    @given(pod=pod_with_oom())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_oom_pod_classified_as_oom(self, pod):
        """Pods with OOMKilled containers are classified as OOM_KILLED."""
        result = classify_pod_failure(pod)
        assert result is not None
        assert result.reason.value == "OOMKilled"

    @given(pod=pod_with_image_pull_error())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_image_pull_pod_classified_as_image_pull(self, pod):
        """Pods with ImagePullBackOff are classified as IMAGE_PULL_BACK_OFF."""
        result = classify_pod_failure(pod)
        assert result is not None
        assert result.reason.value == "ImagePullBackOff"

    @given(pod=pod_with_create_container_error())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_create_container_error_classified_correctly(self, pod):
        """Pods with CreateContainerError are classified as CREATE_CONTAINER_ERROR."""
        result = classify_pod_failure(pod)
        assert result is not None
        assert result.reason.value == "CreateContainerError"

    @given(pod=st.one_of(pod_with_sandbox_error(), pod_with_sandbox_error_message()))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_sandbox_error_classified_correctly(self, pod):
        """Pods with sandbox errors are classified as SANDBOX_ERROR."""
        result = classify_pod_failure(pod)
        assert result is not None
        assert result.reason.value == "SandboxError"

    @given(pod=any_failed_pod)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_classification_is_exactly_one_reason(self, pod):
        """Each pod is classified into exactly one reason (not multiple)."""
        result = classify_pod_failure(pod)

        if result is not None:
            # The result has exactly one reason with a valid value
            reason_value = result.reason.value
            assert reason_value in VALID_REASON_VALUES, (
                f"Reason value '{reason_value}' not in valid set"
            )
            # Verify it matches exactly one valid reason
            matching_reasons = [r for r in VALID_REASON_VALUES if r == reason_value]
            assert len(matching_reasons) == 1, (
                f"Expected exactly one matching reason, got {len(matching_reasons)}"
            )
