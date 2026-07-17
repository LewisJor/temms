"""
Local adaptive inference controller.

This module evaluates local state, chooses the model that should run in a slot,
applies the hot-swap, and falls back if the selected model cannot load.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from temms.conditions.store import ConditionStore
from temms.core.cache import CachedModel, ModelCache
from temms.observability import policy_decision_count, swap_latency_ms
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager, SlotState

if TYPE_CHECKING:
    from temms.inference.runtime import InferenceRuntime

logger = logging.getLogger(__name__)


class ActivationPreflightBlocked(RuntimeError):
    """Raised when edge readiness refuses a local activation."""

    def __init__(
        self,
        message: str,
        *,
        readiness: dict[str, Any] | None = None,
        blocking_gates: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.readiness = readiness or {}
        self.blocking_gates = blocking_gates or []


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
        activation_preflight: Callable[..., dict[str, Any] | None] | None = None,
    ):
        self.slot_manager = slot_manager
        self.condition_store = condition_store
        self.policy_engine = policy_engine
        self.model_cache = model_cache
        self.inference_runtime = inference_runtime
        self.activation_preflight = activation_preflight

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
            activation_preflight = self._activation_preflight(
                slot_name=slot_name,
                model_id=model.id,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                conditions=conditions,
            )
            # Only surface LOADING on a cold start (no model is serving yet). On
            # a hot-swap the previous model keeps serving until the new one is
            # loaded and warmed, so the slot must stay RUNNING to avoid a window
            # where inference is rejected even though a model is available
            # (swap-contract Tier 1).
            if from_model is None:
                self.slot_manager.update_slot_state(slot_name, SlotState.LOADING)
            swap_started = time.monotonic()
            await self.inference_runtime.load_model(slot_name, model.id)
            audit_metadata = self._model_audit_metadata(model.id)
            if activation_preflight:
                audit_metadata["activation_preflight"] = activation_preflight
            self.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=model.id,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                conditions=conditions,
                audit_metadata=audit_metadata,
            )
            swap_latency_ms.observe((time.monotonic() - swap_started) * 1000.0)
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
            preflight_blocked = isinstance(exc, ActivationPreflightBlocked)
            logger.error("Selected model load failed for %s: %s", slot_name, exc)
            return await self._fallback(
                slot_name=slot_name,
                selected_model=model.id,
                trigger_detail=trigger_detail,
                from_model=from_model,
                conditions=conditions,
                load_error=str(exc),
                preserve_slot_state_on_failure=preflight_blocked,
            )

    async def _fallback(
        self,
        slot_name: str,
        selected_model: str,
        trigger_detail: str,
        from_model: str | None,
        conditions: dict[str, Any],
        load_error: str,
        preserve_slot_state_on_failure: bool = False,
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
                activation_preflight = self._activation_preflight(
                    slot_name=slot_name,
                    model_id=model.id,
                    trigger_type="fallback",
                    trigger_detail=trigger_detail,
                    conditions=conditions,
                )
                await self.inference_runtime.load_model(slot_name, model.id)
                audit_metadata = {
                    **self._model_audit_metadata(model.id),
                    "fallback": {
                        "selected_model": selected_model,
                        "attempted": attempted,
                        "failures": failures,
                    },
                }
                if activation_preflight:
                    audit_metadata["activation_preflight"] = activation_preflight
                self.slot_manager.activate_model(
                    slot_name=slot_name,
                    model_id=model.id,
                    trigger_type="fallback",
                    trigger_detail=f"fallback after {trigger_detail}",
                    conditions=conditions,
                    audit_metadata=audit_metadata,
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

        self.slot_manager.update_slot_state(
            slot_name,
            SlotState.RUNNING if preserve_slot_state_on_failure else SlotState.ERROR,
        )
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
        return self.model_cache.get_model(model_id_or_name) or self.model_cache.find_model(
            model_id_or_name
        )

    def _activation_preflight(
        self,
        *,
        slot_name: str,
        model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.activation_preflight is None:
            return None
        return self.activation_preflight(
            slot_name=slot_name,
            model_id=model_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            conditions=conditions,
        )

    def _model_audit_metadata(self, model_id: str) -> dict[str, Any]:
        """Return compact model/package context for decision evidence."""
        model = self.model_cache.get_model(model_id)
        if model is None:
            return {"model_id": model_id}

        package_metadata: dict[str, Any] = {"package_id": model.package_id}
        get_package = getattr(self.model_cache, "get_package", None)
        package = get_package(model.package_id) if callable(get_package) else None
        if package is not None:
            manifest = package.manifest or {}
            import_audit = manifest.get("_temms_import")
            if not isinstance(import_audit, dict):
                import_audit = {}
            signature = import_audit.get("signature")
            signature_summary = None
            if isinstance(signature, dict):
                signature_summary = {
                    "schema_version": signature.get("schema_version"),
                    "algorithm": signature.get("algorithm"),
                    "signer": signature.get("signer"),
                    "key_fingerprint": signature.get("key_fingerprint"),
                    "signed_at": signature.get("signed_at"),
                    "manifest_sha256": signature.get("manifest_sha256"),
                }

            package_metadata.update(
                {
                    "name": package.name,
                    "version": package.version,
                    "source_registry": manifest.get("source_registry"),
                    "mlflow_run_id": manifest.get("mlflow_run_id"),
                    "provenance": manifest.get("provenance", {}),
                    "compatibility": manifest.get("compatibility", {}),
                    "signature_verified": import_audit.get("signature_verified"),
                    "signature": signature_summary,
                }
            )

        return {
            "model_id": model.id,
            "model_name": model.name,
            "model_version": model.version,
            "model_format": model.format.value,
            "model_sha256": model.sha256,
            "package_id": model.package_id,
            "package": package_metadata,
            "provenance": model.metadata.get("provenance", {}),
            "runtime_constraints": model.metadata.get("runtime_constraints", {}),
            "benchmark": model.metadata.get("benchmark", {}),
        }
