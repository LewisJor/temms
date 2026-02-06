"""
Policy evaluation engine.
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
import logging

from temms.policy.schema import SlotPolicy, PolicyRule, Condition, ConditionGroup
from temms.conditions.store import ConditionStore

logger = logging.getLogger(__name__)


class PolicyEngine:
    """Evaluates policies against current conditions."""

    def __init__(self, condition_store: ConditionStore):
        """
        Initialize policy engine.

        Args:
            condition_store: ConditionStore instance
        """
        self.condition_store = condition_store
        self.loaded_policies: Dict[str, SlotPolicy] = {}

    def load_policy(self, policy: SlotPolicy) -> None:
        """Load a policy into the engine."""
        policy_id = f"{policy.spec.slot}/{policy.metadata.name}"
        self.loaded_policies[policy_id] = policy
        logger.info(f"Loaded policy: {policy_id}")

    def load_policy_from_file(self, path: Path) -> SlotPolicy:
        """Load policy from YAML file."""
        policy = SlotPolicy.from_yaml(path)
        self.load_policy(policy)
        return policy

    def evaluate_slot(self, slot_name: str) -> Optional[str]:
        """
        Evaluate all policies for a slot and determine if model should switch.

        Args:
            slot_name: Slot to evaluate

        Returns:
            Model name to switch to, or None if no switch needed
        """
        # Get all policies for this slot
        slot_policies = [
            p for p in self.loaded_policies.values()
            if p.spec.slot == slot_name
        ]

        if not slot_policies:
            return None

        # Sort rules by priority (highest first)
        all_rules = []
        for policy in slot_policies:
            for rule in policy.spec.rules:
                all_rules.append((policy, rule))

        all_rules.sort(key=lambda x: x[1].priority, reverse=True)

        # Evaluate rules in priority order
        for policy, rule in all_rules:
            if self._evaluate_rule(rule):
                logger.info(
                    f"Policy rule matched: {policy.metadata.name}/{rule.name} "
                    f"-> {rule.action.switch_to}"
                )
                return rule.action.switch_to

        return None

    def _evaluate_rule(self, rule: PolicyRule) -> bool:
        """Evaluate a single rule."""
        return self._evaluate_condition_group(rule.conditions)

    def _evaluate_condition_group(self, group: ConditionGroup) -> bool:
        """Evaluate a condition group (AND/OR logic)."""
        # AND logic (all conditions must match)
        if group.all:
            return all(self._evaluate_condition(c) for c in group.all)

        # OR logic (any condition must match)
        if group.any:
            return any(self._evaluate_condition(c) for c in group.any)

        return False

    def _evaluate_condition(self, condition: Condition) -> bool:
        """Evaluate a single condition."""
        # Get current value from condition store
        cond_value = self.condition_store.get(condition.metric)

        if cond_value is None:
            logger.debug(f"Condition metric not found: {condition.metric}")
            return False

        # Check confidence threshold
        if cond_value.confidence < condition.min_confidence:
            logger.debug(
                f"Condition confidence too low: {cond_value.confidence} < {condition.min_confidence}"
            )
            return False

        # Evaluate operator
        value = cond_value.value
        target = condition.value
        op = condition.operator

        try:
            if op == "eq":
                return value == target
            elif op == "neq":
                return value != target
            elif op == "gt":
                return value > target
            elif op == "gte":
                return value >= target
            elif op == "lt":
                return value < target
            elif op == "lte":
                return value <= target
            elif op == "in":
                return value in target
            elif op == "not_in":
                return value not in target
            else:
                logger.warning(f"Unknown operator: {op}")
                return False
        except Exception as e:
            logger.warning(f"Error evaluating condition: {e}")
            return False

    def get_fallback_chain(self, slot_name: str) -> List[str]:
        """Get fallback chain for a slot."""
        for policy in self.loaded_policies.values():
            if policy.spec.slot == slot_name:
                return policy.spec.fallback_chain
        return []

    def list_policies(self) -> List[SlotPolicy]:
        """List all loaded policies."""
        return list(self.loaded_policies.values())
