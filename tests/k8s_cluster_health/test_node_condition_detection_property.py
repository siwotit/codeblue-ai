"""Property-based test for node condition detection completeness.

**Validates: Requirements 3.3**

Property 8: Node condition detection completeness
For any set of nodes with varying conditions, verify all conditions are detected and reported.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import given, settings

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from skills.shared.models import NodeConditionType

# Import functions under test
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "k8s-cluster-health"),
)
from k8s_cluster_health import process_nodes

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "node-condition-check"),
)
from node_condition_check import process_node_conditions


# --- Strategies ---

# All 5 condition types that must be handled
PRESSURE_CONDITION_TYPES = ["MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"]
ALL_CONDITION_TYPES = PRESSURE_CONDITION_TYPES + ["Ready"]

# Map from raw condition type string to the expected NodeConditionType enum value
EXPECTED_CONDITION_MAP = {
    "MemoryPressure": NodeConditionType.MEMORY_PRESSURE,
    "DiskPressure": NodeConditionType.DISK_PRESSURE,
    "PIDPressure": NodeConditionType.PID_PRESSURE,
    "NetworkUnavailable": NodeConditionType.NETWORK_UNAVAILABLE,
}


@st.composite
def node_condition_strategy(draw):
    """Generate a single K8s node condition entry."""
    cond_type = draw(st.sampled_from(ALL_CONDITION_TYPES))
    status = draw(st.sampled_from(["True", "False"]))
    return {
        "type": cond_type,
        "status": status,
        "lastTransitionTime": "2024-06-15T10:00:00Z",
        "message": draw(st.text(min_size=0, max_size=50, alphabet=st.characters(
            whitelist_categories=("L", "N", "P", "Z"),
        ))),
    }


@st.composite
def node_strategy(draw):
    """Generate a single K8s node with a set of conditions.

    Ensures each condition type appears at most once per node (matching real K8s behavior).
    """
    node_name = draw(st.text(
        min_size=1,
        max_size=30,
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-"),
    ))

    # Each node has a subset of condition types, each appearing at most once
    available_types = draw(st.permutations(ALL_CONDITION_TYPES))
    num_conditions = draw(st.integers(min_value=1, max_value=len(ALL_CONDITION_TYPES)))
    selected_types = available_types[:num_conditions]

    conditions = []
    for cond_type in selected_types:
        status = draw(st.sampled_from(["True", "False"]))
        conditions.append({
            "type": cond_type,
            "status": status,
            "lastTransitionTime": "2024-06-15T10:00:00Z",
            "message": f"{cond_type} condition",
        })

    return {
        "metadata": {"name": node_name},
        "status": {"conditions": conditions},
    }


@st.composite
def nodes_list_strategy(draw):
    """Generate a list of K8s nodes (1 to 10 nodes)."""
    return draw(st.lists(node_strategy(), min_size=1, max_size=10))


# --- Property Tests ---


class TestNodeConditionDetectionCompleteness:
    """Property 8: Node condition detection completeness.

    **Validates: Requirements 3.3**

    For any set of nodes with varying conditions, verify all conditions
    are detected and reported.
    """

    @given(nodes=nodes_list_strategy())
    @settings(max_examples=200)
    def test_all_pressure_conditions_detected_by_process_nodes(self, nodes):
        """Every pressure condition set to True is detected by process_nodes.

        For each node, any condition in PRESSURE_CONDITION_TYPES with status "True"
        must appear in the result's conditions list.
        """
        result = process_nodes(nodes)

        # Collect expected conditions: (node_name, condition_type) pairs where status is True
        expected_pressure = set()
        for node in nodes:
            node_name = node["metadata"]["name"]
            for cond in node["status"]["conditions"]:
                if cond["type"] in PRESSURE_CONDITION_TYPES and cond["status"] == "True":
                    expected_pressure.add((node_name, EXPECTED_CONDITION_MAP[cond["type"]]))

        # Collect actual detected conditions
        detected_pressure = set()
        for c in result.conditions:
            if c.condition != NodeConditionType.NOT_READY:
                detected_pressure.add((c.node_name, c.condition))

        assert expected_pressure == detected_pressure, (
            f"Missed pressure conditions: {expected_pressure - detected_pressure}, "
            f"Extra conditions: {detected_pressure - expected_pressure}"
        )

    @given(nodes=nodes_list_strategy())
    @settings(max_examples=200)
    def test_not_ready_nodes_detected_by_process_nodes(self, nodes):
        """Every node where Ready condition has status != True is detected as NotReady.

        Per Requirement 3.3, NotReady nodes must be reported.
        """
        result = process_nodes(nodes)

        # Collect expected not-ready nodes
        expected_not_ready = set()
        for node in nodes:
            node_name = node["metadata"]["name"]
            for cond in node["status"]["conditions"]:
                if cond["type"] == "Ready" and cond["status"] != "True":
                    expected_not_ready.add(node_name)

        # Collect actual not-ready detections
        detected_not_ready = set()
        for c in result.conditions:
            if c.condition == NodeConditionType.NOT_READY:
                detected_not_ready.add(c.node_name)

        assert expected_not_ready == detected_not_ready, (
            f"Missed NotReady nodes: {expected_not_ready - detected_not_ready}, "
            f"Extra NotReady nodes: {detected_not_ready - expected_not_ready}"
        )

    @given(nodes=nodes_list_strategy())
    @settings(max_examples=200)
    def test_all_conditions_detected_by_process_node_conditions(self, nodes):
        """Every condition set to True is detected by process_node_conditions.

        The dedicated node-condition-check skill must also detect all 5 condition types.
        """
        result = process_node_conditions(nodes)

        # Collect all expected conditions (both pressure and NotReady)
        expected = set()
        for node in nodes:
            node_name = node["metadata"]["name"]
            for cond in node["status"]["conditions"]:
                cond_type = cond["type"]
                cond_status = cond["status"]

                if cond_type in PRESSURE_CONDITION_TYPES and cond_status == "True":
                    expected.add((node_name, EXPECTED_CONDITION_MAP[cond_type]))
                elif cond_type == "Ready" and cond_status != "True":
                    expected.add((node_name, NodeConditionType.NOT_READY))

        # Collect actual detected conditions
        detected = set()
        for c in result:
            detected.add((c.node_name, c.condition))

        assert expected == detected, (
            f"Missed conditions: {expected - detected}, "
            f"Extra conditions: {detected - expected}"
        )

    @given(nodes=nodes_list_strategy())
    @settings(max_examples=200)
    def test_all_five_condition_types_are_handled(self, nodes):
        """The system handles all 5 condition types without errors.

        Verify that processing nodes with any combination of the 5 condition types
        (NotReady, MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable)
        never produces an unexpected condition type.
        """
        result = process_node_conditions(nodes)

        valid_conditions = {
            NodeConditionType.NOT_READY,
            NodeConditionType.MEMORY_PRESSURE,
            NodeConditionType.DISK_PRESSURE,
            NodeConditionType.PID_PRESSURE,
            NodeConditionType.NETWORK_UNAVAILABLE,
        }

        for c in result:
            assert c.condition in valid_conditions, (
                f"Unexpected condition type: {c.condition}"
            )
            # All detected conditions should have status=True
            assert c.status is True, (
                f"Detected condition should have status=True, got {c.status}"
            )

    @given(nodes=nodes_list_strategy())
    @settings(max_examples=200)
    def test_process_nodes_and_process_node_conditions_consistent(self, nodes):
        """Both functions detect the same set of conditions for the same input.

        process_nodes (from k8s_cluster_health) and process_node_conditions
        (from node_condition_check) should agree on detected conditions.
        """
        nodes_result = process_nodes(nodes)
        conditions_result = process_node_conditions(nodes)

        # Extract (node_name, condition) pairs from both
        from_process_nodes = {(c.node_name, c.condition) for c in nodes_result.conditions}
        from_process_node_conditions = {(c.node_name, c.condition) for c in conditions_result}

        assert from_process_nodes == from_process_node_conditions, (
            f"process_nodes detected: {from_process_nodes}, "
            f"process_node_conditions detected: {from_process_node_conditions}"
        )
