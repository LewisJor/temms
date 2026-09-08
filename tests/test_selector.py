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


def test_keeps_current_model_when_priority_is_tied() -> None:
    current = ModelRef(id="z-current", digest="sha256:current")
    other = ModelRef(id="a-other", digest="sha256:other")
    selector = BestFeasibleSelector(
        [ModelPolicy(other, priority=10), ModelPolicy(current, priority=10)]
    )

    decision = selector.select(
        slot="vision",
        models=[other, current],
        runtime=RuntimeState(active_model=current),
        context={},
    )

    assert decision.selected_model == current
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
