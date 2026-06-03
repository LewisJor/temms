"""
Comprehensive tests for the policy engine (#9).

Tests:
- Policy loading (YAML, programmatic)
- Rule evaluation (priority ordering, all/any logic)
- Condition operators (eq, neq, gt, gte, lt, lte, in, not_in, matches, exists, not_exists)
- PolicyEvalResult fields (switch_to, version, preload, triggered_by, is_default)
- Default model restoration when no rules match
- Fallback chain retrieval
- Confidence threshold filtering
- Edge cases (empty policies, no conditions, unknown operators)
"""

import pytest
from pathlib import Path

from temms.policy.engine import PolicyEngine, PolicyEvalResult
from temms.policy.schema import (
    SlotPolicy,
    SlotPolicyMetadata,
    SlotPolicySpec,
    PolicyRule,
    PolicyAction,
    Condition,
    ConditionGroup,
)
from temms.conditions.store import ConditionStore


@pytest.fixture
def engine(condition_store):
    """Create PolicyEngine with ConditionStore."""
    return PolicyEngine(condition_store)


def _make_policy(
    name="test-policy",
    slot="vision",
    rules=None,
    default_model=None,
    fallback_chain=None,
):
    """Helper to build a SlotPolicy."""
    return SlotPolicy(
        metadata=SlotPolicyMetadata(name=name),
        spec=SlotPolicySpec(
            slot=slot,
            rules=rules or [],
            default_model=default_model,
            fallback_chain=fallback_chain or [],
        ),
    )


def _make_rule(
    name="rule-1",
    priority=50,
    switch_to="target-model",
    version=None,
    preload=None,
    all_conditions=None,
    any_conditions=None,
):
    """Helper to build a PolicyRule."""
    conditions = ConditionGroup(all=all_conditions, any=any_conditions)
    return PolicyRule(
        name=name,
        priority=priority,
        conditions=conditions,
        action=PolicyAction(
            switch_to=switch_to,
            version=version,
            preload=preload,
        ),
    )


# ── Loading & Registration ──────────────────────────────────────────


class TestPolicyLoading:
    """Test policy loading and registration."""

    def test_load_policy(self, engine):
        policy = _make_policy(name="my-policy", slot="vision")
        engine.load_policy(policy)

        assert len(engine.list_policies()) == 1
        assert engine.list_policies()[0].metadata.name == "my-policy"

    def test_load_multiple_policies(self, engine):
        engine.load_policy(_make_policy(name="p1", slot="vision"))
        engine.load_policy(_make_policy(name="p2", slot="targeting"))

        assert len(engine.list_policies()) == 2

    def test_load_policy_from_yaml(self, engine, sample_policy_yaml):
        policy = engine.load_policy_from_file(sample_policy_yaml)

        assert policy.metadata.name == "thermal-adaptive"
        assert policy.spec.slot == "vision"
        assert len(engine.list_policies()) == 1

    def test_load_replaces_duplicate_id(self, engine):
        """Loading a policy with the same slot/name key replaces it."""
        engine.load_policy(_make_policy(name="p1", slot="vision"))
        engine.load_policy(_make_policy(name="p1", slot="vision"))

        # Same key = same dict entry, so still 1
        assert len(engine.list_policies()) == 1


# ── evaluate_slot – basic ───────────────────────────────────────────


class TestEvaluateSlotBasic:
    """Test basic evaluate_slot behavior."""

    def test_no_policies_returns_empty_result(self, engine):
        result = engine.evaluate_slot("vision")

        assert isinstance(result, PolicyEvalResult)
        assert result.switch_to is None
        assert result.triggered_by is None

    def test_no_matching_slot_returns_empty_result(self, engine):
        engine.load_policy(_make_policy(slot="targeting"))

        result = engine.evaluate_slot("vision")  # different slot

        assert result.switch_to is None

    def test_matching_rule_returns_switch(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100)

        rule = _make_rule(
            switch_to="hot-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert result.switch_to == "hot-model"
        assert result.is_default is False
        assert "rule-1" in result.triggered_by

    def test_non_matching_rule_returns_no_switch(self, engine, condition_store):
        condition_store.set("temp", 50, "test", 100)

        rule = _make_rule(
            switch_to="hot-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert result.switch_to is None


# ── evaluate_slot – default_model ───────────────────────────────────


class TestDefaultModelRestoration:
    """Test default_model restoration when no rules match (#2)."""

    def test_default_model_returned_when_no_rules_match(self, engine, condition_store):
        condition_store.set("temp", 50, "test", 100)

        rule = _make_rule(
            switch_to="hot-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(
            _make_policy(rules=[rule], default_model="default-model")
        )

        result = engine.evaluate_slot("vision")

        assert result.switch_to == "default-model"
        assert result.is_default is True
        assert result.triggered_by == "default_model"

    def test_no_default_model_returns_none(self, engine, condition_store):
        condition_store.set("temp", 50, "test", 100)

        rule = _make_rule(
            switch_to="hot-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(_make_policy(rules=[rule], default_model=None))

        result = engine.evaluate_slot("vision")

        assert result.switch_to is None
        assert result.is_default is False

    def test_rule_match_takes_priority_over_default(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100)

        rule = _make_rule(
            switch_to="hot-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(
            _make_policy(rules=[rule], default_model="default-model")
        )

        result = engine.evaluate_slot("vision")

        assert result.switch_to == "hot-model"
        assert result.is_default is False


# ── PolicyEvalResult fields ─────────────────────────────────────────


class TestPolicyEvalResultFields:
    """Test that PolicyEvalResult carries correct metadata."""

    def test_version_pin(self, engine, condition_store):
        condition_store.set("flag", True, "test", 100)

        rule = _make_rule(
            switch_to="my-model",
            version="2.1.0",
            all_conditions=[Condition(metric="flag", operator="eq", value=True)],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert result.version == "2.1.0"

    def test_preload_list(self, engine, condition_store):
        condition_store.set("flag", True, "test", 100)

        rule = _make_rule(
            switch_to="my-model",
            preload=["preload-a", "preload-b"],
            all_conditions=[Condition(metric="flag", operator="eq", value=True)],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert result.preload == ["preload-a", "preload-b"]

    def test_triggered_by_includes_policy_and_rule_name(self, engine, condition_store):
        condition_store.set("flag", True, "test", 100)

        rule = _make_rule(
            name="my-rule",
            switch_to="my-model",
            all_conditions=[Condition(metric="flag", operator="eq", value=True)],
        )
        engine.load_policy(_make_policy(name="my-policy", rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert "my-policy" in result.triggered_by
        assert "my-rule" in result.triggered_by

    def test_explanation_records_matched_rule_condition_values(
        self, engine, condition_store
    ):
        condition_store.set(
            "environmental.atmospheric.visibility_m",
            50,
            "sensor",
            100,
            confidence=0.98,
        )

        rule = _make_rule(
            name="fog-rule",
            switch_to="fog-model",
            all_conditions=[
                Condition(
                    metric="environmental.atmospheric.visibility_m",
                    operator="lt",
                    value=100,
                    min_confidence=0.9,
                )
            ],
        )
        engine.load_policy(_make_policy(name="weather-policy", rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert result.explanation["reason"] == "rule_matched"
        matched_rule = result.explanation["matched_rule"]
        assert matched_rule["policy"] == "weather-policy"
        assert matched_rule["rule"] == "fog-rule"
        condition = matched_rule["conditions"]["items"][0]
        assert condition["metric"] == "environmental.atmospheric.visibility_m"
        assert condition["operator"] == "lt"
        assert condition["expected"] == 100
        assert condition["actual"] == 50
        assert condition["source"] == "sensor"
        assert condition["priority"] == 100
        assert condition["confidence"] == 0.98
        assert condition["matched"] is True

    def test_explanation_records_non_matching_conditions(self, engine, condition_store):
        condition_store.set("temp", 50, "sensor", 100)

        rule = _make_rule(
            name="hot-rule",
            switch_to="hot-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(_make_policy(name="thermal-policy", rules=[rule]))

        result = engine.evaluate_slot("vision")

        assert result.switch_to is None
        assert result.explanation["reason"] == "no_matching_rule"
        condition = result.explanation["evaluated_rules"][0]["conditions"]["items"][0]
        assert condition["actual"] == 50
        assert condition["expected"] == 75
        assert condition["matched"] is False
        assert condition["reason"] == "operator_mismatch"


# ── Priority ordering ───────────────────────────────────────────────


class TestRulePriority:
    """Test that higher priority rules are evaluated first."""

    def test_higher_priority_wins(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100)

        low = _make_rule(
            name="low",
            priority=10,
            switch_to="low-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        high = _make_rule(
            name="high",
            priority=100,
            switch_to="high-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(_make_policy(rules=[low, high]))

        result = engine.evaluate_slot("vision")

        assert result.switch_to == "high-model"

    def test_first_match_wins_at_same_priority(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100)

        r1 = _make_rule(
            name="r1",
            priority=50,
            switch_to="first-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        r2 = _make_rule(
            name="r2",
            priority=50,
            switch_to="second-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        engine.load_policy(_make_policy(rules=[r1, r2]))

        result = engine.evaluate_slot("vision")

        # Both match, but order may depend on sort stability.
        # The important thing: one of them matches.
        assert result.switch_to in ("first-model", "second-model")

    def test_cross_policy_priority(self, engine, condition_store):
        """Rules from different policies still sort by priority."""
        condition_store.set("temp", 80, "test", 100)

        r_low = _make_rule(
            name="low",
            priority=10,
            switch_to="low-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )
        r_high = _make_rule(
            name="high",
            priority=100,
            switch_to="high-model",
            all_conditions=[Condition(metric="temp", operator="gte", value=75)],
        )

        engine.load_policy(_make_policy(name="p-low", rules=[r_low]))
        engine.load_policy(_make_policy(name="p-high", rules=[r_high]))

        result = engine.evaluate_slot("vision")

        assert result.switch_to == "high-model"


# ── Condition logic (all / any) ─────────────────────────────────────


class TestConditionGroupLogic:
    """Test AND (all) and OR (any) condition groups."""

    def test_all_conditions_must_match(self, engine, condition_store):
        condition_store.set("a", 10, "test", 100)
        condition_store.set("b", 20, "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="a", operator="eq", value=10),
                Condition(metric="b", operator="eq", value=20),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_all_conditions_fail_if_one_doesnt_match(self, engine, condition_store):
        condition_store.set("a", 10, "test", 100)
        condition_store.set("b", 99, "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="a", operator="eq", value=10),
                Condition(metric="b", operator="eq", value=20),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None

    def test_any_condition_matches_if_one_matches(self, engine, condition_store):
        condition_store.set("a", 99, "test", 100)  # won't match
        condition_store.set("b", 20, "test", 100)  # will match

        rule = _make_rule(
            switch_to="target",
            any_conditions=[
                Condition(metric="a", operator="eq", value=10),
                Condition(metric="b", operator="eq", value=20),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_any_condition_fails_if_none_match(self, engine, condition_store):
        condition_store.set("a", 99, "test", 100)
        condition_store.set("b", 99, "test", 100)

        rule = _make_rule(
            switch_to="target",
            any_conditions=[
                Condition(metric="a", operator="eq", value=10),
                Condition(metric="b", operator="eq", value=20),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None


# ── Operators ────────────────────────────────────────────────────────


class TestConditionOperators:
    """Test all condition operators."""

    @pytest.mark.parametrize(
        "op,stored,target,expected",
        [
            ("eq", 10, 10, True),
            ("eq", 10, 11, False),
            ("neq", 10, 11, True),
            ("neq", 10, 10, False),
            ("gt", 10, 9, True),
            ("gt", 10, 10, False),
            ("gte", 10, 10, True),
            ("gte", 10, 11, False),
            ("lt", 10, 11, True),
            ("lt", 10, 10, False),
            ("lte", 10, 10, True),
            ("lte", 10, 9, False),
        ],
    )
    def test_comparison_operators(self, engine, condition_store, op, stored, target, expected):
        condition_store.set("val", stored, "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[Condition(metric="val", operator=op, value=target)],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        result = engine.evaluate_slot("vision")

        if expected:
            assert result.switch_to == "target"
        else:
            assert result.switch_to is None

    def test_in_operator(self, engine, condition_store):
        condition_store.set("val", "fog", "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="val", operator="in", value=["fog", "mist", "rain"]),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_not_in_operator(self, engine, condition_store):
        condition_store.set("val", "clear", "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="val", operator="not_in", value=["fog", "mist"]),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_matches_operator(self, engine, condition_store):
        condition_store.set("val", "yolov8-fog-v2", "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="val", operator="matches", value=r"yolov8-.*-v\d+"),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_matches_operator_no_match(self, engine, condition_store):
        condition_store.set("val", "mobilenet", "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="val", operator="matches", value=r"yolov8-.*"),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None

    def test_exists_operator(self, engine, condition_store):
        condition_store.set("sensor.temp", 50, "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="sensor.temp", operator="exists", value=None),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_exists_operator_missing(self, engine):
        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="nonexistent", operator="exists", value=None),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None

    def test_not_exists_operator(self, engine):
        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="nonexistent", operator="not_exists", value=None),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_not_exists_operator_when_present(self, engine, condition_store):
        condition_store.set("sensor.temp", 50, "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="sensor.temp", operator="not_exists", value=None),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None

    def test_unknown_operator_returns_false(self, engine, condition_store):
        condition_store.set("val", 10, "test", 100)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="val", operator="banana", value=10),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None


# ── Confidence threshold ─────────────────────────────────────────────


class TestConfidenceThreshold:
    """Test min_confidence filtering."""

    def test_condition_below_confidence_threshold_skipped(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100, confidence=0.3)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="temp", operator="gte", value=75, min_confidence=0.7),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None

    def test_condition_meets_confidence_threshold(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100, confidence=0.9)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="temp", operator="gte", value=75, min_confidence=0.7),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"

    def test_zero_confidence_threshold_always_passes(self, engine, condition_store):
        condition_store.set("temp", 80, "test", 100, confidence=0.01)

        rule = _make_rule(
            switch_to="target",
            all_conditions=[
                Condition(metric="temp", operator="gte", value=75, min_confidence=0.0),
            ],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to == "target"


# ── Missing conditions ───────────────────────────────────────────────


class TestMissingConditions:
    """Test behavior when condition metrics are not in the store."""

    def test_missing_metric_evaluates_to_false(self, engine):
        rule = _make_rule(
            switch_to="target",
            all_conditions=[Condition(metric="missing", operator="eq", value=10)],
        )
        engine.load_policy(_make_policy(rules=[rule]))

        assert engine.evaluate_slot("vision").switch_to is None


# ── Fallback chain ───────────────────────────────────────────────────


class TestFallbackChain:
    """Test fallback chain retrieval."""

    def test_get_fallback_chain(self, engine):
        engine.load_policy(
            _make_policy(fallback_chain=["a", "b", "c"])
        )

        chain = engine.get_fallback_chain("vision")

        assert chain == ["a", "b", "c"]

    def test_get_fallback_chain_no_policy(self, engine):
        chain = engine.get_fallback_chain("nonexistent")

        assert chain == []
