"""DDIL-specific metrics (issue #30)."""

from __future__ import annotations

from temms.observability import (
    decision_chain_length_gauge,
    model_swaps_total,
    offline_mode_gauge,
    pending_intents_gauge,
    seconds_since_hub_sync_gauge,
    set_ddil_gauges,
)


def _value(metric):
    return metric._value.get()


def test_model_swaps_counter_increments_on_activation(slot_manager):
    before = _value(model_swaps_total)
    slot_manager.create_slot("vision", "Vision", default_model="a")
    slot_manager.activate_model("vision", "a", "startup", "seed")
    slot_manager.activate_model("vision", "b", "policy", "fog")
    assert _value(model_swaps_total) == before + 2


def test_decision_count_is_cheap_and_correct(slot_manager):
    slot_manager.create_slot("vision", "Vision", default_model="a")
    assert slot_manager.decision_count() == 0
    slot_manager.activate_model("vision", "a", "startup", "seed")
    slot_manager.activate_model("vision", "b", "policy", "fog")
    assert slot_manager.decision_count() == 2


def test_set_ddil_gauges_updates_all():
    set_ddil_gauges(
        offline=True, pending_intents=3, decision_chain_length=7, seconds_since_sync=12.0
    )
    assert _value(offline_mode_gauge) == 1
    assert _value(pending_intents_gauge) == 3
    assert _value(decision_chain_length_gauge) == 7
    assert _value(seconds_since_hub_sync_gauge) == 12.0

    set_ddil_gauges(
        offline=False, pending_intents=0, decision_chain_length=7, seconds_since_sync=None
    )
    assert _value(offline_mode_gauge) == 0
    assert _value(pending_intents_gauge) == 0
    # None leaves the previous sync gauge untouched.
    assert _value(seconds_since_hub_sync_gauge) == 12.0
