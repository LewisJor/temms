"""
Local adaptive inference controller.

This module evaluates local state, chooses the model that should run in a slot,
applies the hot-swap, and falls back if the selected model cannot load.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from temms.conditions.store import ConditionStore
from temms.core.cache import CachedModel, ModelCache
from temms.observability import policy_decision_count
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager, SlotState

if TYPE_CHECKING:
    from temms.inference.runtime import InferenceRuntime

logger = logging.getLogger(__name__)


@dataclass
class AdaptiveDecision:
    """Result of evaluating and optionally applying one slot decision."""

    slot: str
    status: str
    trigger_type: str
    trigger_detail: str
    from_model: str | None = None
    selected_model: str | None = None
    activated_model: str | None = None
    fallback_attempted: list[str] = field(default_factory=list)
    fallback_failures: list[str] = field(default_factory=list)
    conditions: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "trigger_detail": self.trigger_detail,
            "from_model": self.from_model,
            "selected_model": self.selected_model,
            "activated_model": self.activated_model,
            "fallback_attempted": self.fallback_attempted,
            "fallback_failures": self.fallback_failures,
            "conditions": self.conditions,
            "applied": self.applied,
            "reason": self.reason,
        }


class AdaptiveInferenceController:
    """Evaluates local policy/operator state and applies model activations."""

    def __init__(
        self,
        slot_manager: SlotManager,
        condition_store: ConditionStore,
        policy_engine: PolicyEngine,
        model_cache: ModelCache,
        inference_runtime: InferenceRuntime,
    ):
        self.slot_manager = slot_manager
        self.condition_store = condition_store
        self.policy_engine = policy_engine
        self.model_cache = model_cache
        self.inference_runtime = inference_runtime

    async def evaluate_slot(self, slot_name: str, apply: bool = True) -> AdaptiveDecision:
        """Evaluate one slot and optionally apply the selected activation."""
        slot = self.slot_manager.get_slot(slot_name)
        conditions = self.condition_store.get_snapshot()
        if slot is None:
            return AdaptiveDecision(
                slot=slot_name,
                status="slot_not_found",
                trigger_type="none",
                trigger_detail="slot_not_found",
                conditions=conditions,
                reason=f"Slot not found: {slot_name}",
            )

        from_model = slot.active_model_id

        if self.slot_manager.has_active_override(slot.name):
            refreshed_slot = self.slot_manager.get_slot(slot.name)
            override = refreshed_slot.operator_override if refreshed_slot else None
            if override is None:
                return AdaptiveDecision(
                    slot=slot.name,
                    status="no_change",
                    trigger_type="operator",
                    trigger_detail="override_expired",
                    from_model=from_model,
                    conditions=conditions,
                    reason="Operator override expired before evaluation",
                )
            model = self._resolve_model(override.model_id)
            if model is None:
                return AdaptiveDecision(
                    slot=slot.name,
                    status="model_not_found",
                    trigger_type="operator",
                    trigger_detail=override.reason or "operator_override",
                    from_model=from_model,
                    selected_model=override.model_id,
                    conditions=conditions,
                    reason=f"Override model not found: {override.model_id}",
                )
            if model.id == from_model:
                return AdaptiveDecision(
                    slot=slot.name,
                    status="override_active",
                    trigger_type="operator",
                    trigger_detail=override.reason or "operator_override",
                    from_model=from_model,
                    selected_model=model.id,
                    activated_model=from_model,
                    conditions=conditions,
                    reason="Operator override already active",
                )
            return await self._apply_or_preview(
                slot_name=slot.name,
                model=model,
                trigger_type="operator",
                trigger_detail=override.reason or "operator_override",
                conditions=conditions,
                from_model=from_model,
                apply=apply,
            )

        policy_decision_count.inc()
        result = self.policy_engine.evaluate_slot(slot.name)
        if result.switch_to is None:
            return AdaptiveDecision(
                slot=slot.name,
                status="no_decision",
                trigger_type="policy",
                trigger_detail="no_matching_policy",
                from_model=from_model,
                conditions=conditions,
                reason="No policy selected a different model",
            )

        model = self.model_cache.find_model(result.switch_to, version=result.version)
        trigger_detail = result.triggered_by or "policy_evaluation"
        if model is None:
            return AdaptiveDecision(
                slot=slot.name,
                status="model_not_found",
                trigger_type="policy",
                trigger_detail=trigger_detail,
                from_model=from_model,
                selected_model=result.switch_to,
                conditions=conditions,
                reason=f"Policy selected missing model: {result.switch_to}",
            )

        if model.id == from_model:
            return AdaptiveDecision(
                slot=slot.name,
                status="no_change",
                trigger_type="policy",
                trigger_detail=trigger_detail,
                from_model=from_model,
                selected_model=model.id,
                activated_model=from_model,
                conditions=conditions,
                reason="Selected model is already active",
            )

        return await self._apply_or_preview(
            slot_name=slot.name,
            model=model,
            trigger_type="policy",
            trigger_detail=trigger_detail,
            conditions=conditions,
            from_model=from_model,
            apply=apply,
        )

    async def evaluate_all_running(self, apply: bool = True) -> list[AdaptiveDecision]:
        """Evaluate all running slots."""
        decisions = []
        for slot in self.slot_manager.list_slots():
            if slot.state != SlotState.RUNNING:
                continue
            decisions.append(await self.evaluate_slot(slot.name, apply=apply))
        return decisions

    async def _apply_or_preview(
        self,
        slot_name: str,
        model: CachedModel,
        trigger_type: str,
        trigger_detail: str,
        conditions: dict[str, Any],
        from_model: str | None,
        apply: bool,
    ) -> AdaptiveDecision:
        if not apply:
            return AdaptiveDecision(
                slot=slot_name,
                status="selected",
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                from_model=from_model,
                selected_model=model.id,
                conditions=conditions,
                reason="Model selected; apply=false",
            )

        try:
            self.slot_manager.update_slot_state(slot_name, SlotState.LOADING)
            await self.inference_runtime.load_model(slot_name, model.id)
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=model.id,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                conditions=conditions,
            )
            logger.info(
                "Adaptive activation: slot=%s %s -> %s trigger=%s/%s",
                slot_name,
                from_model,
                model.id,
                trigger_type,
                trigger_detail,
            )
            return AdaptiveDecision(
                slot=slot_name,
                status="activated",
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                from_model=from_model,
                selected_model=model.id,
                activated_model=model.id,
                conditions=conditions,
                applied=True,
            )
        except Exception as exc:
            logger.error("Selected model load failed for %s: %s", slot_name, exc)
            return await self._fallback(
                slot_name=slot_name,
                selected_model=model.id,
                trigger_detail=trigger_detail,
                from_model=from_model,
                conditions=conditions,
                load_error=str(exc),
            )

    async def _fallback(
        self,
        slot_name: str,
        selected_model: str,
        trigger_detail: str,
        from_model: str | None,
        conditions: dict[str, Any],
        load_error: str,
    ) -> AdaptiveDecision:
        fallback_chain = self.policy_engine.get_fallback_chain(slot_name)
        attempted = []
        failures = [f"{selected_model}: {load_error}"]

        for model_name in fallback_chain:
            model = self.model_cache.find_model(model_name)
            if model is None:
                attempted.append(model_name)
                failures.append(f"{model_name}: not found")
                continue
            attempted.append(model.id)
            try:
                await self.inference_runtime.load_model(slot_name, model.id)
                self.slot_manager.activate_model(
                    slot_name=slot_name,
                    model_id=model.id,
                    trigger_type="fallback",
                    trigger_detail=f"fallback after {trigger_detail}",
                    conditions=conditions,
                )
                return AdaptiveDecision(
                    slot=slot_name,
                    status="fallback_activated",
                    trigger_type="fallback",
                    trigger_detail=f"fallback after {trigger_detail}",
                    from_model=from_model,
                    selected_model=selected_model,
                    activated_model=model.id,
                    fallback_attempted=attempted,
                    fallback_failures=failures,
                    conditions=conditions,
                    applied=True,
                )
            except Exception as exc:
                failures.append(f"{model.id}: {exc}")

        self.slot_manager.update_slot_state(slot_name, SlotState.ERROR)
        return AdaptiveDecision(
            slot=slot_name,
            status="failed",
            trigger_type="fallback",
            trigger_detail=f"fallback after {trigger_detail}",
            from_model=from_model,
            selected_model=selected_model,
            fallback_attempted=attempted,
            fallback_failures=failures,
            conditions=conditions,
            reason="Selected model and all fallback models failed",
        )

    def _resolve_model(self, model_id_or_name: str) -> CachedModel | None:
        return (
            self.model_cache.get_model(model_id_or_name)
            or self.model_cache.find_model(model_id_or_name)
        )
