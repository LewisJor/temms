from itertools import permutations

import pytest

from temms import (
    BestFeasibleSelector,
    Constraint,
    ModelPolicy,
    ModelRef,
    Operator,
    RuntimeState,
    ValueSource,
)


def test_selects_highest_priority_feasible_model() -> None:
    large = ModelRef(id="large", digest="sha256:large")
    small = ModelRef(id="small", digest="sha256:small")
    selector = BestFeasibleSelector(
        [
            ModelPolicy(
                large,
                priority=100,
                constraints=(
                    Constraint(
                        "memory_mb",
                        Operator.GTE,
                        512,
                        source=ValueSource.RESOURCE,
                    ),
                ),
            ),
            ModelPolicy(small, priority=10),
        ]
    )

    decision = selector.select(
        slot="vision",
        models=[large, small],
        runtime=RuntimeState(active_model=None, resources={"memory_mb": 256}),
        context={},
    )

    assert decision.selected_model == small
    assert decision.evaluations[0].feasible is False
    assert "resource.memory_mb" in decision.evaluations[0].failures[0]


@pytest.mark.parametrize(
    "has_current,priority,enabled,available,expected_index",
    [
        pytest.param(False, 10, True, True, 0, id="id-then-digest"),
        pytest.param(True, 10, True, True, 2, id="current-wins-tie"),
        pytest.param(True, 9, True, True, 0, id="priority-before-current"),
        pytest.param(True, 10, False, True, 0, id="infeasible-current"),
        pytest.param(True, 10, True, False, 0, id="unavailable-current"),
    ],
)
def test_ranking_is_independent_of_policy_and_inventory_order(
    has_current, priority, enabled, available, expected_index
) -> None:
    models = [
        ModelRef("a", "sha256:a"),
        ModelRef("a", "sha256:z"),
        ModelRef("z", "sha256:0"),
    ]
    # Runtime state metadata can differ without changing current identity.
    active = ModelRef("z", "sha256:0", metadata={"source": "state"}) if has_current else None
    policies = [
        ModelPolicy(models[0], priority=10),
        ModelPolicy(models[1], priority=10),
        ModelPolicy(models[2], priority=priority,
                    constraints=(Constraint("enabled", Operator.EQ, True),)),
    ]
    inventory = models if available else models[:2]
    for policy_order in permutations(policies):
        selector = BestFeasibleSelector(policy_order)
        for model_order in permutations(inventory):
            decision = selector.select(
                slot="vision",
                models=model_order,
                runtime=RuntimeState(active_model=active),
                context={"enabled": enabled},
            )
            assert decision.selected_model == models[expected_index]
            if expected_index == 2:
                assert decision.reason == "current model remains the best feasible model"


def test_returns_explicit_no_feasible_model() -> None:
    model = ModelRef(id="night", digest="sha256:night")
    selector = BestFeasibleSelector(
        [
            ModelPolicy(
                model,
                constraints=(Constraint("light", Operator.EQ, "low"),),
            )
        ]
    )

    decision = selector.select(
        slot="vision",
        models=[model],
        runtime=RuntimeState(active_model=None),
        context={"light": "bright"},
    )

    assert decision.selected_model is None
    assert decision.reason == "no feasible model"


def test_unavailable_policy_model_is_explicitly_infeasible() -> None:
    missing = ModelRef(id="missing", digest="sha256:missing")
    decision = BestFeasibleSelector([ModelPolicy(missing)]).select(
        slot="vision",
        models=[],
        runtime=RuntimeState(active_model=None),
        context={},
    )

    assert decision.selected_model is None
    assert decision.evaluations[0].failures == ("model is unavailable from the runtime",)


def test_runtime_inventory_does_not_define_policy() -> None:
    configured = ModelRef(id="configured", digest="sha256:configured")
    unconfigured = ModelRef(id="unconfigured", digest="sha256:unconfigured")
    decision = BestFeasibleSelector([ModelPolicy(configured)]).select(
        slot="vision",
        models=[configured, unconfigured],
        runtime=RuntimeState(active_model=None),
        context={},
    )

    assert decision.selected_model == configured
    assert [item.model for item in decision.evaluations] == [configured]


def test_duplicate_policy_identity_is_rejected() -> None:
    model = ModelRef(id="model", digest="sha256:model")

    with pytest.raises(ValueError, match="unique model identities"):
        BestFeasibleSelector([ModelPolicy(model), ModelPolicy(model)])


def test_in_operator_requires_membership() -> None:
    model = ModelRef(id="weather", digest="sha256:weather")
    selector = BestFeasibleSelector(
        [
            ModelPolicy(
                model,
                constraints=(Constraint("weather", Operator.IN, ("fog", "rain")),),
            )
        ]
    )

    decision = selector.select(
        slot="vision",
        models=[model],
        runtime=RuntimeState(active_model=None),
        context={"weather": "fog"},
    )

    assert decision.selected_model == model


@pytest.mark.parametrize("source", [ValueSource.CONTEXT, ValueSource.RESOURCE])
@pytest.mark.parametrize(
    "values,operator,expected,feasible",
    [
        pytest.param({}, Operator.GTE, 40, False, id="missing"),
        pytest.param({"reading": 40}, Operator.GTE, 40, True, id="inclusive-boundary"),
        pytest.param({"reading": 39}, Operator.GTE, 40, False, id="below-boundary"),
        pytest.param({"reading": "40"}, Operator.GTE, 40, False, id="no-ordering-coercion"),
        pytest.param({"reading": "40"}, Operator.EQ, 40, False, id="no-equality-coercion"),
        pytest.param({"reading": True}, Operator.EQ, 1, True, id="python-bool-equality"),
        pytest.param({"reading": float("nan")}, Operator.GTE, 40, False, id="nan-ordering"),
        pytest.param({"reading": float("nan")}, Operator.NE, 40, True, id="nan-inequality"),
        pytest.param({"reading": float("inf")}, Operator.GTE, 40, True, id="positive-infinity"),
        pytest.param({"reading": float("-inf")}, Operator.GTE, 40, False, id="negative-infinity"),
        pytest.param({"reading": "rain"}, Operator.IN, ("rain", "fog"), True, id="membership"),
        pytest.param({"reading": "rain"}, Operator.NOT_IN, ("rain",), False, id="not-in"),
        pytest.param({"reading": "rain"}, Operator.IN, "rain", False, id="string-container"),
        pytest.param({"reading": 40}, Operator.NOT_IN, 40, False, id="non-container"),
    ],
)
def test_constraint_inputs_are_characterized_without_coercion(
    source, values, operator, expected, feasible
) -> None:
    model = ModelRef("model", "sha256:model")
    selector = BestFeasibleSelector([
        ModelPolicy(model, constraints=(Constraint("reading", operator, expected, source),)),
    ])
    # A matching value in the other namespace must not repair missing/invalid input.
    other = {"reading": 40}
    context, resources = (values, other) if source is ValueSource.CONTEXT else (other, values)
    decision = selector.select(
        slot="vision", models=[model],
        runtime=RuntimeState(active_model=None, resources=resources), context=context,
    )

    assert decision.selected_model == (model if feasible else None)
    evaluation, = decision.evaluations
    assert evaluation.feasible is feasible
    if feasible:
        assert evaluation.failures == ()
    elif not values:
        assert evaluation.failures == (f"{source.value}.reading is missing",)
    else:
        failure, = evaluation.failures
        assert failure.startswith(f"{source.value}.reading=")
        assert "does not satisfy" in failure
