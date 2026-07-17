"""
Policy evaluation engine.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Dict, Any
from pathlib import Path
import logging
import re
import time

from temms.policy.schema import SlotPolicy, PolicyRule, Condition, ConditionGroup
from temms.conditions.store import ConditionStore

logger = logging.getLogger(__name__)


@dataclass
class PolicyEvalResult:
    """Result of policy evaluation for a slot."""
    switch_to: Optional[str] = None  # Model name to switch to
    version: Optional[str] = None  # Optional version pin
    preload: List[str] = field(default_factory=list)  # Models to preload
    triggered_by: Optional[str] = None  # Rule name or "default_model"
    is_default: bool = False  # True if returning to default model
    explanation: Dict[str, Any] = field(default_factory=dict)


class PolicyEngine:
    """Evaluates policies against current conditions."""

    def __init__(
        self,
        condition_store: ConditionStore,
        time_fn: Optional[Callable[[], float]] = None,
    ):
        """
        Initialize policy engine.

        Args:
            condition_store: ConditionStore instance
            time_fn: monotonic clock source for dwell/hysteresis timing. Defaults
                to time.monotonic (immune to wall-clock jumps — important under
                DDIL). Injectable for deterministic tests.
        """
        self.condition_store = condition_store
        self.loaded_policies: Dict[str, SlotPolicy] = {}
        self._time_fn = time_fn or time.monotonic
        # rule id -> monotonic timestamp when its conditions became (and have
        # since stayed) continuously satisfied. Cleared when they stop matching.
        self._rule_satisfied_since: Dict[str, float] = {}

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

    def evaluate_slot(self, slot_name: str) -> PolicyEvalResult:
        """
        Evaluate all policies for a slot and determine if model should switch.

        Args:
            slot_name: Slot to evaluate

        Returns:
            PolicyEvalResult with switch_to model, version, preload list, and trigger info
        """
        # Get all policies for this slot
        slot_policies = [
            p for p in self.loaded_policies.values()
            if p.spec.slot == slot_name
        ]

        if not slot_policies:
            return PolicyEvalResult()

        # Sort rules by priority (highest first)
        all_rules = []
        for policy in slot_policies:
            for rule in policy.spec.rules:
                all_rules.append((policy, rule))

        all_rules.sort(key=lambda x: x[1].priority, reverse=True)

        evaluated_rules: list[dict[str, Any]] = []
        now = self._time_fn()

        # Evaluate rules in priority order
        for policy, rule in all_rules:
            matched, explanation = self._explain_rule(policy, rule)
            eligible = self._apply_dwell(policy, rule, matched, now, explanation)
            evaluated_rules.append(explanation)
            # A rule whose conditions match but has not yet dwelled long enough is
            # "pending": it does not fire, damping flapping. Evaluation falls
            # through to lower-priority rules / the default model.
            if eligible:
                logger.info(
                    f"Policy rule matched: {policy.metadata.name}/{rule.name} "
                    f"-> {rule.action.switch_to}"
                )
                return PolicyEvalResult(
                    switch_to=rule.action.switch_to,
                    version=rule.action.version,
                    preload=rule.action.preload or [],
                    triggered_by=f"{policy.metadata.name}/{rule.name}",
                    is_default=False,
                    explanation={
                        "reason": "rule_matched",
                        "matched_rule": explanation,
                        "evaluated_rules": evaluated_rules,
                    },
                )

        # No rule matched - check for default_model across slot policies
        for policy in slot_policies:
            if policy.spec.default_model is not None:
                logger.info(
                    f"No rules matched for slot {slot_name}, "
                    f"returning default model: {policy.spec.default_model}"
                )
                return PolicyEvalResult(
                    switch_to=policy.spec.default_model,
                    triggered_by="default_model",
                    is_default=True,
                    explanation={
                        "reason": "default_model",
                        "policy": policy.metadata.name,
                        "slot": policy.spec.slot,
                        "default_model": policy.spec.default_model,
                        "evaluated_rules": evaluated_rules,
                    },
                )

        return PolicyEvalResult(
            explanation={
                "reason": "no_matching_rule",
                "slot": slot_name,
                "evaluated_rules": evaluated_rules,
            }
        )

    def _apply_dwell(
        self,
        policy: SlotPolicy,
        rule: PolicyRule,
        matched: bool,
        now: float,
        explanation: dict[str, Any],
    ) -> bool:
        """Gate a matched rule on its dwell window, annotating the explanation.

        Returns whether the rule is *eligible* to fire: its conditions match AND
        have held continuously for at least ``min_dwell_s``. A matched rule that
        has not dwelled long enough is "pending" and does not fire.
        """
        rule_id = f"{policy.metadata.name}/{rule.name}"
        dwell = float(rule.min_dwell_s or 0.0)

        if not matched:
            self._rule_satisfied_since.pop(rule_id, None)
            explanation["dwell"] = {
                "min_dwell_s": dwell,
                "satisfied_for_s": 0.0,
                "eligible": False,
                "pending": False,
            }
            return False

        since = self._rule_satisfied_since.setdefault(rule_id, now)
        satisfied_for = max(0.0, now - since)
        eligible = satisfied_for >= dwell
        explanation["dwell"] = {
            "min_dwell_s": dwell,
            "satisfied_for_s": satisfied_for,
            "eligible": eligible,
            "pending": not eligible,
        }
        return eligible

    def _evaluate_rule(self, rule: PolicyRule) -> bool:
        """Evaluate a single rule (ignores dwell; conditions only)."""
        matched, _ = self._explain_condition_group(rule.conditions)
        return matched

    def _explain_rule(
        self,
        policy: SlotPolicy,
        rule: PolicyRule,
    ) -> tuple[bool, dict[str, Any]]:
        """Evaluate a rule and return structured explainability metadata."""
        matched, condition_group = self._explain_condition_group(rule.conditions)
        return matched, {
            "policy": policy.metadata.name,
            "slot": policy.spec.slot,
            "rule": rule.name,
            "priority": rule.priority,
            "matched": matched,
            "action": {
                "switch_to": rule.action.switch_to,
                "version": rule.action.version,
                "preload": rule.action.preload or [],
            },
            "conditions": condition_group,
        }

    def _evaluate_condition_group(self, group: ConditionGroup) -> bool:
        """Evaluate a condition group (AND/OR logic)."""
        matched, _ = self._explain_condition_group(group)
        return matched

    def _explain_condition_group(
        self,
        group: ConditionGroup,
    ) -> tuple[bool, dict[str, Any]]:
        """Evaluate a condition group and return per-condition evidence."""
        # AND logic (all conditions must match)
        if group.all:
            condition_results = [self._explain_condition(c) for c in group.all]
            return all(c["matched"] for c in condition_results), {
                "mode": "all",
                "items": condition_results,
            }

        # OR logic (any condition must match)
        if group.any:
            condition_results = [self._explain_condition(c) for c in group.any]
            return any(c["matched"] for c in condition_results), {
                "mode": "any",
                "items": condition_results,
            }

        return False, {"mode": "none", "items": []}

    def _evaluate_condition(self, condition: Condition) -> bool:
        """Evaluate a single condition."""
        return self._explain_condition(condition)["matched"]

    def _explain_condition(self, condition: Condition) -> dict[str, Any]:
        """Evaluate one condition and return the values used for audit."""
        evidence: dict[str, Any] = {
            "metric": condition.metric,
            "operator": condition.operator,
            "expected": condition.value,
            "min_confidence": condition.min_confidence,
            "actual": None,
            "source": None,
            "priority": None,
            "confidence": None,
            "updated_at": None,
            "matched": False,
            "reason": None,
        }

        # Handle exists/not_exists before value lookup
        if condition.operator == "exists":
            cond_value = self.condition_store.get(condition.metric)
            if cond_value is not None:
                evidence.update(_condition_value_evidence(cond_value))
                evidence["matched"] = True
            else:
                evidence["reason"] = "missing"
            return evidence
        if condition.operator == "not_exists":
            cond_value = self.condition_store.get(condition.metric)
            if cond_value is None:
                evidence["matched"] = True
            else:
                evidence.update(_condition_value_evidence(cond_value))
                evidence["reason"] = "present"
            return evidence

        # Get current value from condition store
        cond_value = self.condition_store.get(condition.metric)

        if cond_value is None:
            logger.debug(f"Condition metric not found: {condition.metric}")
            evidence["reason"] = "missing"
            return evidence

        evidence.update(_condition_value_evidence(cond_value))

        # Check confidence threshold
        if cond_value.confidence < condition.min_confidence:
            logger.debug(
                f"Condition confidence too low: {cond_value.confidence} < {condition.min_confidence}"
            )
            evidence["reason"] = "low_confidence"
            return evidence

        # Evaluate operator
        value = cond_value.value
        target = condition.value
        op = condition.operator

        try:
            if op == "eq":
                matched = value == target
            elif op == "neq":
                matched = value != target
            elif op == "gt":
                matched = value > target
            elif op == "gte":
                matched = value >= target
            elif op == "lt":
                matched = value < target
            elif op == "lte":
                matched = value <= target
            elif op == "in":
                matched = value in target
            elif op == "not_in":
                matched = value not in target
            elif op == "matches":
                matched = bool(re.search(str(target), str(value)))
            else:
                logger.warning(f"Unknown operator: {op}")
                evidence["reason"] = "unknown_operator"
                return evidence
            evidence["matched"] = matched
            if not matched:
                evidence["reason"] = "operator_mismatch"
            return evidence
        except Exception as e:
            logger.warning(f"Error evaluating condition: {e}")
            evidence["reason"] = f"evaluation_error: {e}"
            return evidence

    def get_fallback_chain(self, slot_name: str) -> List[str]:
        """Get fallback chain for a slot."""
        for policy in self.loaded_policies.values():
            if policy.spec.slot == slot_name:
                return policy.spec.fallback_chain
        return []

    def list_policies(self) -> List[SlotPolicy]:
        """List all loaded policies."""
        return list(self.loaded_policies.values())

    def clear_policies(self) -> None:
        """Clear all loaded policies before reloading from the active policy store."""
        self.loaded_policies.clear()
        self._rule_satisfied_since.clear()


def _condition_value_evidence(cond_value: Any) -> dict[str, Any]:
    """Return serializable condition value metadata for policy decisions."""
    return {
        "actual": cond_value.value,
        "source": cond_value.source,
        "priority": cond_value.priority,
        "confidence": cond_value.confidence,
        "updated_at": cond_value.updated_at.isoformat(),
    }
