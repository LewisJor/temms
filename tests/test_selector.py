from temms import (
    BestFeasibleSelector,
    Constraint,
    ModelRef,
    Operator,
    RuntimeState,
    ValueSource,
)


def test_selects_highest_priority_feasible_model() -> None:
    large = ModelRef(
        id="large",
        digest="sha256:large",
        priority=100,
        constraints=(
            Constraint(
                "memory_mb",
                Operator.GTE,
                512,
                source=ValueSource.RESOURCE,
            ),
        ),
    )
    small = ModelRef(id="small", digest="sha256:small", priority=10)

    decision = BestFeasibleSelector().select(
        slot="vision",
        models=[large, small],
        runtime=RuntimeState(active_model=None, resources={"memory_mb": 256}),
        context={},
    )

    assert decision.selected_model == small
    assert decision.evaluations[0].feasible is False
    assert "resource.memory_mb" in decision.evaluations[0].failures[0]


def test_keeps_current_model_when_priority_is_tied() -> None:
    current = ModelRef(id="z-current", digest="sha256:current", priority=10)
    other = ModelRef(id="a-other", digest="sha256:other", priority=10)

    decision = BestFeasibleSelector().select(
        slot="vision",
        models=[other, current],
        runtime=RuntimeState(active_model=current),
        context={},
    )

    assert decision.selected_model == current
    assert decision.reason == "current model remains the best feasible model"


def test_returns_explicit_no_feasible_model() -> None:
    model = ModelRef(
        id="night",
        digest="sha256:night",
        constraints=(Constraint("light", Operator.EQ, "low"),),
    )

    decision = BestFeasibleSelector().select(
        slot="vision",
        models=[model],
        runtime=RuntimeState(active_model=None),
        context={"light": "bright"},
    )

    assert decision.selected_model is None
    assert decision.reason == "no feasible model"


def test_in_operator_requires_membership() -> None:
    model = ModelRef(
        id="weather",
        digest="sha256:weather",
        constraints=(Constraint("weather", Operator.IN, ("fog", "rain")),),
    )

    decision = BestFeasibleSelector().select(
        slot="vision",
        models=[model],
        runtime=RuntimeState(active_model=None),
        context={"weather": "fog"},
    )

    assert decision.selected_model == model
