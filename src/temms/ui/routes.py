"""
TEMMS Web UI routes using FastAPI + Jinja2 + HTMX.

Provides a lightweight dashboard for monitoring and controlling the TEMMS daemon.
No Node.js or frontend build step required — HTMX and Tailwind CSS loaded via CDN.
"""

import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

# Templates directory (relative to this file)
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _template_response(request: Request, name: str, context: Dict[str, Any]) -> HTMLResponse:
    context.setdefault("request", request)
    return templates.TemplateResponse(request, name, context)


def create_ui_router(get_state_func, control_auth_dependency=None) -> APIRouter:
    """
    Create the UI router with state dependency injection.

    Args:
        get_state_func: Dependency function that returns AppState
        control_auth_dependency: Optional dependency used to protect UI writes

    Returns:
        Configured APIRouter
    """
    router = APIRouter(prefix="/ui", tags=["ui"])
    write_dependencies = (
        [Depends(control_auth_dependency)] if control_auth_dependency is not None else []
    )

    # ---- Dashboard ----

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, state=Depends(get_state_func)):
        """Main UI entrypoint.

        Hub-configured deployments land on the guided operator flow; standalone
        agent installs keep the legacy technical dashboard as their root view.
        """
        if getattr(state, "hub_lite", None) is not None:
            return _template_response(request, "operate.html", _operate_ui_context(request, state))
        return _template_response(request, "dashboard.html", _dashboard_ui_context(request, state))

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request, state=Depends(get_state_func)):
        """Technical dashboard with system overview."""
        return _template_response(request, "dashboard.html", _dashboard_ui_context(request, state))

    # ---- Slots ----

    @router.get("/slots", response_class=HTMLResponse)
    async def slots_page(request: Request, state=Depends(get_state_func)):
        """List all slots."""
        slots = state.slot_manager.list_slots()
        return _template_response(
            request,
            "slots.html",
            {
                "request": request,
                "slots": slots,
            },
        )

    @router.get("/slots/{slot_name}", response_class=HTMLResponse)
    async def slot_detail(
        request: Request,
        slot_name: str,
        state=Depends(get_state_func),
    ):
        """Slot detail with override controls."""
        slot = state.slot_manager.get_slot(slot_name)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"Slot not found: {slot_name}")

        # Get available models
        models = state.model_cache.list_models()

        # Get override status
        has_override = state.slot_manager.has_active_override(slot_name)

        # Get recent decisions for this slot
        decisions = state.slot_manager.get_decision_log(slot_name, limit=10)

        return _template_response(
            request,
            "slot_detail.html",
            {
                "request": request,
                "slot": slot,
                "models": models,
                "has_override": has_override,
                "decisions": decisions,
            },
        )

    @router.post(
        "/slots/{slot_name}/override",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def set_override(
        request: Request,
        slot_name: str,
        model_name: str = Form(...),
        reason: str = Form(""),
        state=Depends(get_state_func),
    ):
        """Set operator override on a slot (HTMX form submission)."""
        # Find model by name
        model = state.model_cache.find_model(model_name)
        if model is None:
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Model not found: {model_name}</div>'
            )

        try:
            # Load model
            await state.inference_runtime.load_model(slot_name, model.id)

            # Set override
            state.slot_manager.set_operator_override(
                slot_name=slot_name,
                model_id=model.id,
                reason=reason or "UI override",
                source="web_ui",
            )

            # Activate
            state.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=model.id,
                trigger_type="operator",
                trigger_detail=f"UI override: {reason}" if reason else "UI override",
                conditions=state.condition_store.get_snapshot(),
                audit_metadata={
                    "model_id": model.id,
                    "model_name": model.name,
                    "model_version": model.version,
                    "model_format": model.format.value,
                    "model_sha256": model.sha256,
                    "package_id": model.package_id,
                    "provenance": model.metadata.get("provenance", {}),
                },
            )

            return HTMLResponse(f'<div class="text-green-600 p-2">Override set: {model_name}</div>')
        except Exception as e:
            return HTMLResponse(f'<div class="text-red-600 p-2">Error: {e}</div>')

    @router.post(
        "/slots/{slot_name}/clear-override",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def clear_override(
        request: Request,
        slot_name: str,
        state=Depends(get_state_func),
    ):
        """Clear operator override on a slot."""
        state.slot_manager.clear_operator_override(slot_name)
        return HTMLResponse('<div class="text-green-600 p-2">Override cleared</div>')

    # ---- Conditions ----

    @router.get("/conditions", response_class=HTMLResponse)
    async def conditions_page(request: Request, state=Depends(get_state_func)):
        """List all conditions with injection form."""
        conditions = list(state.condition_store.get_all().values())
        return _template_response(
            request,
            "conditions.html",
            {
                "request": request,
                "conditions": conditions,
            },
        )

    @router.post(
        "/conditions/inject",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def inject_condition(
        request: Request,
        path: str = Form(...),
        value: str = Form(...),
        state=Depends(get_state_func),
    ):
        """Inject a condition value (HTMX form submission)."""
        # Try to parse value as number
        parsed_value: Any = value
        try:
            parsed_value = float(value)
            if parsed_value == int(parsed_value):
                parsed_value = int(parsed_value)
        except ValueError:
            pass  # Keep as string

        state.condition_store.set(
            path=path,
            value=parsed_value,
            source="web_ui",
            priority=1000,
            confidence=1.0,
        )

        return HTMLResponse(
            f'<div class="text-green-600 p-2">Condition set: {path} = {parsed_value}</div>'
        )

    @router.post(
        "/conditions/clear-overrides",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def clear_condition_overrides(
        request: Request,
        state=Depends(get_state_func),
    ):
        """Clear all operator condition overrides."""
        count = state.condition_store.clear_operator_overrides()
        return HTMLResponse(
            f'<div class="text-green-600 p-2">Cleared {count} operator overrides</div>'
        )

    # ---- Decisions ----

    @router.get("/decisions", response_class=HTMLResponse)
    async def decisions_page(
        request: Request,
        slot: Optional[str] = None,
        state=Depends(get_state_func),
    ):
        """Decision log."""
        if slot:
            decisions = state.slot_manager.get_decision_log(slot, limit=50)
        else:
            # Get decisions from all slots
            all_slots = state.slot_manager.list_slots()
            decisions = []
            for s in all_slots:
                decisions.extend(state.slot_manager.get_decision_log(s.name, limit=50))
            decisions.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            decisions = decisions[:50]

        slots = state.slot_manager.list_slots()

        return _template_response(
            request,
            "decisions.html",
            {
                "request": request,
                "decisions": decisions,
                "slots": slots,
                "selected_slot": slot,
            },
        )

    # ---- Models ----

    @router.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request, state=Depends(get_state_func)):
        """List cached models."""
        models = state.model_cache.list_models()
        return _template_response(
            request,
            "models.html",
            {
                "request": request,
                "models": models,
            },
        )

    # ---- Import ----

    @router.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request, state=Depends(get_state_func)):
        """Package import page."""
        packages = state.model_cache.list_packages()
        return _template_response(
            request,
            "import_page.html",
            {
                "request": request,
                "packages": packages,
            },
        )

    @router.post(
        "/import",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def do_import(
        request: Request,
        package_path: str = Form(...),
        state=Depends(get_state_func),
    ):
        """Import a package from the given path."""
        from temms.core.package import PackageImporter
        from temms.core.signing import read_signing_key

        path = Path(package_path)
        if not path.exists():
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Path not found: {package_path}</div>'
            )

        try:
            daemon_config = state.daemon_config
            require_signature = bool(getattr(daemon_config, "rollout_require_signature", True))
            signing_key = (
                read_signing_key(
                    getattr(daemon_config, "rollout_signing_key", None),
                    getattr(daemon_config, "rollout_signing_key_file", None),
                )
                if daemon_config is not None
                else None
            )
            importer = PackageImporter(
                cache_dir=path.parent,
                model_cache=state.model_cache,
                storage=state.model_storage,
                active_policy_dir=(
                    state.daemon_config.policy_dir if state.daemon_config is not None else None
                ),
                require_signature=require_signature,
                signing_key=signing_key,
                device_profile=getattr(daemon_config, "hub_device_profile", None),
            )
            result = importer.import_package(path, verify=True)
            reloaded_policies = _reload_active_policy_store(state)

            msg = (
                f"Imported {len(result.models)} models, "
                f"{len(result.policies)} policies from {result.manifest.name}"
            )
            if state.daemon_config is not None and state.daemon_config.policy_dir is not None:
                msg = f"{msg}; active policies reloaded: {reloaded_policies}"
            return HTMLResponse(f'<div class="text-green-600 p-2">{msg}</div>')
        except Exception as e:
            return HTMLResponse(f'<div class="text-red-600 p-2">Import failed: {e}</div>')

    # ---- Guided Operations ----

    @router.get("/operate", response_class=HTMLResponse)
    async def operate_page(request: Request, state=Depends(get_state_func)):
        """Guided model-to-edge workflow for the primary MVP operator path."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            raise HTTPException(status_code=404, detail="Hub Lite is not configured")

        return _template_response(request, "operate.html", _operate_ui_context(request, state))

    @router.get("/runtimes", response_class=HTMLResponse)
    async def runtime_catalog_page(request: Request, state=Depends(get_state_func)):
        """Runtime/container catalog for edge simulation targets."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            raise HTTPException(status_code=404, detail="Hub Lite is not configured")

        return _template_response(request, "runtime_catalog.html", _hub_ui_context(request, state))

    # ---- Hub Lite Operator Console ----

    @router.get("/hub", response_class=HTMLResponse)
    async def hub_page(request: Request, state=Depends(get_state_func)):
        """Hub Lite operator console."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            raise HTTPException(status_code=404, detail="Hub Lite is not configured")

        return _template_response(request, "hub.html", _hub_ui_context(request, state))

    @router.post(
        "/hub/devices/enroll",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_enroll_hub_device(
        request: Request,
        device_id: str = Form("edge-sim"),
        profile: str = Form("x86_64-cpu"),
        site: str = Form("lab"),
        status: str = Form("online"),
        state=Depends(get_state_func),
    ):
        """Enroll a simulated edge device from the UI."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            inventory = _simulated_inventory_for_profile(profile)
            labels = {
                "site": site or "lab",
                "source": "web-ui",
                "simulated": "true",
            }
            device = hub.enroll_device(
                device_id=device_id,
                profile=profile,
                labels=labels,
                inventory=inventory,
            )
            hub.heartbeat(
                device_id=device_id,
                status=status or "online",
                inventory=inventory,
                deployment_status={"state": "READY", "source": "web-ui-sim"},
            )
            return _ui_success(
                f"Enrolled simulated edge device {device.get('device_id')} "
                f"({device.get('profile')})",
                refresh=True,
            )
        except Exception as e:
            return _ui_error(f"Device enrollment failed: {e}")

    @router.post(
        "/hub/packages/register",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_register_hub_package(
        request: Request,
        package_path: str = Form(...),
        strict_metadata: bool = Form(True),
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Register a package artifact in Hub Lite from the UI."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            package = _register_package_from_path(
                state,
                package_path,
                actor=actor,
                strict_metadata=strict_metadata,
            )
            return _ui_success(
                f"Registered package {package.get('package_id')} ({package.get('version')})",
                refresh=True,
            )
        except Exception as e:
            return _ui_error(f"Package registration failed: {e}")

    @router.post(
        "/hub/mlflow/package",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_package_mlflow_model(
        request: Request,
        model_uri: str = Form(...),
        slot: str = Form(...),
        tracking_uri: str = Form(""),
        output_dir: str = Form(""),
        device_profile: str = Form(""),
        model_artifact: str = Form(""),
        allow_missing_schema: bool = Form(False),
        allow_missing_runtime_constraints: bool = Form(False),
        strict_metadata: bool = Form(True),
        register_package: bool = Form(True),
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Build a signed TEMMS package from an MLflow model URI."""
        try:
            from temms.core.package_builder import build_package_from_mlflow

            require_signature, signing_key = _ui_signature_policy(state)
            package_path = build_package_from_mlflow(
                model_uri=model_uri,
                slot=slot,
                policy_path=None,
                output_dir=Path(output_dir) if output_dir else _default_package_output_dir(state),
                tracking_uri=tracking_uri or None,
                device_profile=device_profile or None,
                model_artifact_path=model_artifact or None,
                require_schema=not allow_missing_schema,
                require_runtime_constraints=not allow_missing_runtime_constraints,
                signing_key=signing_key if require_signature else None,
                strict_metadata=strict_metadata,
                archive=True,
            )
            message = f"Built package {package_path}"
            if register_package:
                package = _register_package_from_path(
                    state,
                    str(package_path),
                    actor=actor,
                    strict_metadata=strict_metadata,
                )
                message += f" and registered {package.get('package_id')}"
            return _ui_success(message, refresh=register_package)
        except Exception as e:
            return _ui_error(f"MLflow package build failed: {e}")

    @router.post(
        "/hub/runtime-targets/register",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_register_runtime_target(
        request: Request,
        runtime_target_id: str = Form(...),
        image: str = Form(...),
        name: str = Form(""),
        os_name: str = Form("linux"),
        arch: str = Form(""),
        device_profiles: str = Form(""),
        runtimes: str = Form(""),
        onnx_providers: str = Form(""),
        accelerators: str = Form(""),
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Register a BYO container runtime target from the UI."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            runtime_names = _csv_list(runtimes)
            provider_names = _csv_list(onnx_providers)
            runtime_inventory = {runtime: {"available": True} for runtime in runtime_names}
            if provider_names:
                runtime_inventory.setdefault("onnxruntime", {"available": True})[
                    "providers"
                ] = provider_names
            accelerator_inventory = {
                accelerator: {"available": True} for accelerator in _csv_list(accelerators)
            }
            profiles = _csv_list(device_profiles)
            constraints: dict[str, Any] = {}
            if profiles:
                constraints["device_profiles"] = profiles
            if runtime_names:
                constraints["runtimes"] = runtime_names
            if provider_names:
                constraints["preferred_providers"] = provider_names
            if accelerator_inventory:
                constraints["accelerators"] = list(accelerator_inventory)

            target = hub.upsert_runtime_target(
                {
                    "runtime_target_id": runtime_target_id,
                    "name": name or runtime_target_id,
                    "image": image,
                    "os": os_name,
                    "arch": arch or None,
                    "device_profiles": profiles,
                    "runtimes": runtime_inventory,
                    "accelerators": accelerator_inventory,
                    "runtime_constraints": constraints,
                    "source": "byo",
                },
                actor=actor,
            )
            return _ui_success(
                f"Registered runtime target {target.get('runtime_target_id')} ({target.get('image')})",
                refresh=True,
            )
        except Exception as e:
            return _ui_error(f"Runtime target registration failed: {e}")

    @router.post(
        "/hub/runtime-targets/validate",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_validate_runtime_target(
        request: Request,
        package_id: str = Form(...),
        runtime_target_id: str = Form(...),
        package_path: str = Form(""),
        dry_run: bool = Form(False),
        allow_unsigned_package: bool = Form(False),
        strict_metadata: bool = Form(True),
        pull_image: bool = Form(False),
        timeout_s: int = Form(300),
        state=Depends(get_state_func),
    ):
        """Validate or preview package validation inside a runtime target container."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            from temms.core.runtime_target_runner import validate_runtime_target_package

            package = hub.get_package(package_id)
            if package is None:
                return _ui_error(f"Unknown package: {package_id}")
            runtime_target = hub.get_runtime_target(runtime_target_id)
            if runtime_target is None:
                return _ui_error(f"Unknown runtime target: {runtime_target_id}")

            selected_path = package_path or package.get("path")
            if not selected_path:
                return _ui_error(f"Package {package_id} does not include a catalog path")

            require_signature = not allow_unsigned_package
            signing_key = None
            if require_signature:
                _, signing_key = _ui_signature_policy(state)

            result = validate_runtime_target_package(
                runtime_target,
                Path(selected_path).expanduser(),
                require_signature=require_signature,
                strict_metadata=strict_metadata,
                signing_key=signing_key,
                pull_image=pull_image,
                dry_run=dry_run,
                timeout_s=timeout_s,
            )
            validation_record = hub.record_runtime_validation(
                runtime_target_id,
                result.to_dict(),
                package_id=package_id,
                package_path=str(selected_path),
                actor="operator:web-ui",
            )
            return _ui_runtime_validation_result(
                result.to_dict(),
                signing_key=signing_key,
                validation_record=validation_record,
            )
        except Exception as e:
            return _ui_error(f"Runtime validation failed: {e}")

    @router.post(
        "/hub/compatibility/preview",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_preview_rollout_compatibility(
        request: Request,
        device_id: str = Form(...),
        package_id: str = Form(...),
        runtime_target_id: str = Form(""),
        state=Depends(get_state_func),
    ):
        """Preview rollout compatibility without creating an assignment."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            result = hub.preview_rollout_compatibility(
                device_id=device_id,
                package_id=package_id,
                runtime_target_id=runtime_target_id or None,
            )
            return _ui_compatibility_preview_result(result)
        except Exception as e:
            return _ui_error(f"Compatibility preview failed: {e}")

    @router.post(
        "/hub/deployment-drafts/active",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_save_active_deployment_draft(
        request: Request,
        package_id: str = Form(...),
        runtime_target_id: str = Form(""),
        device_id: str = Form(...),
        slot: str = Form("vision"),
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Save the active operator deployment candidate."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            draft = hub.upsert_deployment_draft(
                "active",
                package_id=package_id,
                runtime_target_id=runtime_target_id or None,
                device_id=device_id,
                slot=slot or None,
                actor=actor,
            )
            return _ui_success(
                f"Saved deployment draft {draft.get('package_id')} -> {draft.get('device_id')}",
                refresh=True,
            )
        except Exception as e:
            return _ui_error(f"Deployment draft save failed: {e}")

    @router.post(
        "/hub/rollouts/assign",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_assign_rollout(
        request: Request,
        device_id: str = Form(...),
        package_id: str = Form(...),
        slot: str = Form(...),
        rollout_id: str = Form(""),
        runtime_target_id: str = Form(""),
        require_runtime_validation: bool = Form(False),
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Assign a rollout to a target device."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            rollout = hub.assign_rollout(
                device_id=device_id,
                package_id=package_id,
                slot=slot or None,
                rollout_id=rollout_id or None,
                runtime_target_id=runtime_target_id or None,
                require_runtime_validation=require_runtime_validation,
                actor=actor,
            )
            return _ui_success(
                f"Assigned rollout {rollout.get('rollout_id')} to {device_id}/{slot}",
                refresh=True,
            )
        except Exception as e:
            return _ui_error(f"Rollout assignment failed: {e}")

    @router.post(
        "/hub/rollouts/{rollout_id}/apply",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_apply_rollout(
        request: Request,
        rollout_id: str,
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Apply a rollout on the local edge agent."""
        try:
            from temms.inference.server import RolloutApplyRequest, apply_rollout

            result = await apply_rollout(
                rollout_id,
                RolloutApplyRequest(actor=actor),
                request,
                state,
            )
            return _ui_success(
                f"Applied rollout {rollout_id}: {result.get('status')} {result.get('model', '')}",
                refresh=True,
            )
        except HTTPException as e:
            return _ui_error(str(e.detail))
        except Exception as e:
            return _ui_error(f"Rollout apply failed: {e}")

    @router.post(
        "/hub/rollouts/{rollout_id}/rollback",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_rollback_rollout(
        request: Request,
        rollout_id: str,
        reason: str = Form("operator requested rollback"),
        actor: str = Form("operator:web-ui"),
        state=Depends(get_state_func),
    ):
        """Rollback a rollout on the local edge agent."""
        try:
            from temms.inference.server import RolloutRollbackRequest, rollback_rollout

            result = await rollback_rollout(
                rollout_id,
                request,
                RolloutRollbackRequest(reason=reason, actor=actor),
                state,
            )
            return _ui_success(
                f"Rolled back {rollout_id} to {result.get('model', 'previous model')}",
                refresh=True,
            )
        except HTTPException as e:
            return _ui_error(str(e.detail))
        except Exception as e:
            return _ui_error(f"Rollout rollback failed: {e}")

    @router.post(
        "/hub/evidence/export",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_export_evidence(request: Request, state=Depends(get_state_func)):
        """Render a compact evidence bundle preview for the operator console."""
        try:
            from temms.evidence import build_evidence_bundle

            bundle = build_evidence_bundle(
                state,
                decision_limit=100,
                telemetry_limit=1000,
                include_benchmarks=True,
            )
            return _ui_evidence_preview(bundle)
        except Exception as e:
            return _ui_error(f"Evidence export failed: {e}")

    @router.post(
        "/hub/airgap/export",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_export_airgap_bundle(
        request: Request,
        include_packages: bool = Form(False),
        state=Depends(get_state_func),
    ):
        """Render a portable air-gap bundle for copy/export from the Hub UI."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            bundle = hub.export_bundle(include_packages=include_packages)
            return _ui_airgap_bundle_preview(bundle)
        except Exception as e:
            return _ui_error(f"Air-gap export failed: {e}")

    @router.post(
        "/hub/airgap/import",
        response_class=HTMLResponse,
        dependencies=write_dependencies,
    )
    async def ui_import_airgap_bundle(
        request: Request,
        bundle_json: str = Form(...),
        state=Depends(get_state_func),
    ):
        """Import a pasted air-gap bundle from the Hub UI."""
        hub = getattr(state, "hub_lite", None)
        if hub is None:
            return _ui_error("Hub Lite is not configured")

        try:
            bundle = json.loads(bundle_json)
            counts = hub.import_bundle(bundle)
            count_text = ", ".join(
                f"{key}: {value}" for key, value in sorted(counts.items())
            )
            return _ui_success(f"Imported air-gap bundle ({count_text})", refresh=True)
        except json.JSONDecodeError as e:
            return _ui_error(f"Air-gap import failed: invalid JSON ({e.msg})")
        except Exception as e:
            return _ui_error(f"Air-gap import failed: {e}")

    # ---- HTMX Partial Fragments ----

    @router.get("/fragments/slots-summary", response_class=HTMLResponse)
    async def slots_summary_fragment(
        request: Request,
        state=Depends(get_state_func),
    ):
        """HTMX fragment: slot summary cards for auto-refresh."""
        slots = state.slot_manager.list_slots()

        html_parts = []
        for slot in slots:
            state_color = {
                "running": "green",
                "stopped": "gray",
                "loading": "yellow",
                "error": "red",
            }.get(slot.state.value, "gray")

            html_parts.append(f"""
            <div class="bg-white rounded-lg shadow p-4">
                <div class="flex justify-between items-center mb-2">
                    <h3 class="font-semibold text-lg">{slot.name}</h3>
                    <span class="inline-flex items-center px-2 py-1 rounded-full text-xs font-medium bg-{state_color}-100 text-{state_color}-800">
                        {slot.state.value}
                    </span>
                </div>
                <p class="text-sm text-gray-600">{slot.description}</p>
                <p class="text-sm mt-2">
                    <span class="font-medium">Model:</span>
                    <span class="text-blue-600">{slot.active_model_id or "none"}</span>
                </p>
                <a href="/ui/slots/{slot.name}" class="text-sm text-blue-500 hover:underline mt-2 block">Details &rarr;</a>
            </div>
            """)

        return HTMLResponse("".join(html_parts))

    return router


def _ui_signature_policy(state) -> tuple[bool, Optional[str]]:
    from temms.inference.server import rollout_signature_policy

    return rollout_signature_policy(state)


def _dashboard_ui_context(request: Request, state) -> Dict[str, Any]:
    slots = state.slot_manager.list_slots()
    conditions = list(state.condition_store.get_all().values())
    policies = state.policy_engine.list_policies()
    uptime = time.time() - state.start_time

    has_error = any(s.state.value == "error" for s in slots)
    has_degraded = any(s.state.value != "running" and s.required for s in slots)
    if has_error:
        health = "error"
    elif has_degraded:
        health = "degraded"
    else:
        health = "healthy"

    decisions = []
    for slot in slots:
        decisions.extend(state.slot_manager.get_decision_log(slot.name, limit=5))
    decisions.sort(key=lambda d: d.get("created_at", ""), reverse=True)

    return {
        "request": request,
        "health": health,
        "slots": slots,
        "conditions": conditions[:10],
        "conditions_count": len(conditions),
        "policies": policies,
        "decisions": decisions[:10],
        "uptime": uptime,
    }


def _reload_active_policy_store(state) -> int:
    """Reload in-memory policies from the daemon's active policy directory."""
    policy_dir = state.daemon_config.policy_dir if state.daemon_config is not None else None
    if policy_dir is None:
        return 0

    state.policy_engine.clear_policies()
    if not policy_dir.exists():
        return 0

    loaded = 0
    for policy_file in sorted([*policy_dir.glob("*.yaml"), *policy_dir.glob("*.yml")]):
        state.policy_engine.load_policy_from_file(policy_file)
        loaded += 1
    return loaded


def _hub_ui_context(request: Request, state) -> Dict[str, Any]:
    hub = state.hub_lite
    slots = state.slot_manager.list_slots()
    return {
        "request": request,
        "devices": hub.list_devices(),
        "packages": hub.list_packages(),
        "rollouts": hub.list_rollouts(),
        "runtime_targets": hub.list_runtime_targets(),
        "runtime_validations": hub.list_runtime_validations(limit=10),
        "benchmarks": hub.list_benchmarks(limit=25),
        "deployment_draft": hub.get_deployment_draft("active"),
        "slots": slots,
        "default_slot": slots[0].name if slots else "vision",
        "default_tracking_uri": _default_mlflow_tracking_uri(state),
        "default_package_output_dir": str(_default_package_output_dir(state)),
    }


def _operate_ui_context(request: Request, state) -> Dict[str, Any]:
    context = _hub_ui_context(request, state)
    context.update(_mission_selection(request, context))
    context["latest_rollout"] = _latest_rollout_for_mission(
        context["rollouts"],
        package_id=context.get("selected_package_id"),
        runtime_target_id=context.get("selected_runtime_target_id"),
        device_id=context.get("selected_device_id"),
    )
    if context["latest_rollout"] is None and not context.get("mission_selection_explicit"):
        context["latest_rollout"] = _latest_rollout(context["rollouts"])
    context["runtime_validation_ready"] = _runtime_validation_ready(context)
    context["deployment_ready"] = bool(
        context["devices"]
        and context["packages"]
        and context["runtime_targets"]
        and context["runtime_validation_ready"]
    )
    context["deployment_flow"] = _deployment_flow(context)
    context["operate_next_step"] = _operate_next_step(context)
    context["operate_next_step_summary"] = _operate_next_step_summary(context)
    context["proof_checklist"] = _proof_checklist(context)
    return context


def _mission_selection(request: Request, context: Dict[str, Any]) -> dict[str, Any]:
    packages = context.get("packages") or []
    runtime_targets = context.get("runtime_targets") or []
    devices = context.get("devices") or []
    deployment_draft = context.get("deployment_draft")
    latest_rollout = _latest_rollout(context.get("rollouts") or [])
    query = request.query_params
    requested_package_id = (query.get("package_id") or "").strip()
    requested_runtime_target_id = (query.get("runtime_target_id") or "").strip()
    requested_device_id = (query.get("device_id") or "").strip()
    explicit = bool(requested_package_id or requested_runtime_target_id or requested_device_id)

    package_id = (
        requested_package_id
        or (
            deployment_draft.get("package_id")
            if isinstance(deployment_draft, dict)
            else None
        )
        or (latest_rollout.get("package_id") if isinstance(latest_rollout, dict) else None)
    )
    runtime_target_id = (
        requested_runtime_target_id
        or (
            deployment_draft.get("runtime_target_id")
            if isinstance(deployment_draft, dict)
            else None
        )
        or (
            latest_rollout.get("runtime_target_id")
            if isinstance(latest_rollout, dict)
            else None
        )
    )
    device_id = (
        requested_device_id
        or (
            deployment_draft.get("device_id")
            if isinstance(deployment_draft, dict)
            else None
        )
        or (latest_rollout.get("device_id") if isinstance(latest_rollout, dict) else None)
    )

    selected_package = _find_by_id(packages, "package_id", package_id) or (
        packages[0] if packages else None
    )
    selected_runtime_target = _find_by_id(
        runtime_targets,
        "runtime_target_id",
        runtime_target_id,
    ) or (runtime_targets[0] if runtime_targets else None)
    selected_device = _find_by_id(devices, "device_id", device_id) or (
        devices[0] if devices else None
    )

    return {
        "mission_selection_explicit": explicit,
        "mission_selection_saved": isinstance(deployment_draft, dict) and not explicit,
        "selected_package": selected_package,
        "selected_package_id": (
            selected_package.get("package_id") if isinstance(selected_package, dict) else None
        ),
        "selected_runtime_target": selected_runtime_target,
        "selected_runtime_target_id": (
            selected_runtime_target.get("runtime_target_id")
            if isinstance(selected_runtime_target, dict)
            else None
        ),
        "selected_device": selected_device,
        "selected_device_id": (
            selected_device.get("device_id") if isinstance(selected_device, dict) else None
        ),
    }


def _deployment_flow(context: Dict[str, Any]) -> list[dict[str, str]]:
    latest_rollout = context.get("latest_rollout")
    benchmarks = context.get("benchmarks") or []
    package = context.get("selected_package")
    runtime = context.get("selected_runtime_target")
    device = context.get("selected_device")

    rollout_state = str(latest_rollout.get("state", "")) if isinstance(latest_rollout, dict) else ""
    evidence_ready = bool(benchmarks or rollout_state in {"activated", "rolled_back", "failed"})
    return [
        {
            "label": "MLflow Registry",
            "state": "linked",
            "detail": context.get("default_tracking_uri") or "configure tracking URI",
            "status": "ready",
        },
        {
            "label": "Signed Package",
            "state": "ready" if package else "missing",
            "detail": (
                f"{package.get('package_id')} v{package.get('version', '-')}"
                if isinstance(package, dict)
                else "build from MLflow"
            ),
            "status": "ready" if package else "waiting",
        },
        {
            "label": "Runtime Image",
            "state": (
                "validated"
                if context.get("runtime_validation_ready")
                else ("needs validation" if runtime else "missing")
            ),
            "detail": (
                f"{runtime.get('runtime_target_id')} ({runtime.get('os', 'linux')}/{runtime.get('arch', '-')})"
                if isinstance(runtime, dict)
                else "add container target"
            ),
            "status": "ready" if context.get("runtime_validation_ready") else "waiting",
        },
        {
            "label": "Edge Device",
            "state": str(device.get("status", "enrolled")) if isinstance(device, dict) else "missing",
            "detail": (
                f"{device.get('device_id')} ({device.get('profile', '-')})"
                if isinstance(device, dict)
                else "enroll or start agent"
            ),
            "status": "ready" if device else "waiting",
        },
        {
            "label": "Rollout Evidence",
            "state": rollout_state or ("recorded" if benchmarks else "pending"),
            "detail": (
                f"{latest_rollout.get('rollout_id')} -> {latest_rollout.get('state')}"
                if isinstance(latest_rollout, dict)
                else (
                    f"{len(benchmarks)} benchmark{'s' if len(benchmarks) != 1 else ''}"
                    if benchmarks
                    else "apply rollout, then export proof"
                )
            ),
            "status": "ready" if evidence_ready else "waiting",
        },
    ]


def _operate_next_step(context: Dict[str, Any]) -> str:
    if not context.get("packages"):
        return "package"
    if not context.get("runtime_targets"):
        return "runtime"
    if not context.get("runtime_validation_ready"):
        return "validate"
    if not context.get("devices"):
        return "device"
    if not context.get("latest_rollout"):
        return "assign"
    return "evidence"


def _operate_next_step_summary(context: Dict[str, Any]) -> dict[str, str]:
    next_step = _operate_next_step(context)
    summaries = {
        "package": {
            "eyebrow": "Start here",
            "label": "Package a registry model",
            "detail": "Select the MLflow model that should become a signed, deployable TEMMS package.",
            "primary": "Build signed package",
            "secondary": "MLflow remains the source of truth; TEMMS adds edge metadata, signing, and rollout policy.",
        },
        "runtime": {
            "eyebrow": "Runtime needed",
            "label": "Choose the target environment",
            "detail": "Add or select the container image that simulates the customer edge OS, architecture, and runtime stack.",
            "primary": "Add runtime image",
            "secondary": "Use default targets for the demo path, or register customer images for real hardware parity.",
        },
        "validate": {
            "eyebrow": "Prove it runs",
            "label": "Validate against a runtime image",
            "detail": "Run the package inside the selected container image before assigning it to an edge device.",
            "primary": "Run validation",
            "secondary": "This is the compatibility gate between model registry and field deployment.",
        },
        "device": {
            "eyebrow": "Target needed",
            "label": "Enroll an edge target",
            "detail": "Create a simulated edge device or wait for a real device heartbeat to report inventory.",
            "primary": "Enroll simulated edge",
            "secondary": "The demo can use a simulated node; production devices can report the same inventory contract.",
        },
        "assign": {
            "eyebrow": "Ready to deploy",
            "label": "Assign the package to an edge device",
            "detail": "Pick the target device and runtime image, preview compatibility, then create the rollout.",
            "primary": "Assign rollout",
            "secondary": "Assignment records the intended model, device, runtime, slot, and validation evidence.",
        },
        "evidence": {
            "eyebrow": "Operate",
            "label": "Apply the rollout and export evidence",
            "detail": "Activate the rollout locally, roll back if needed, and generate proof for the deployment record.",
            "primary": "Apply or prove",
            "secondary": "Evidence ties together registry source, package signature, runtime validation, and edge action.",
        },
    }
    return summaries[next_step]


def _runtime_validation_ready(context: Dict[str, Any]) -> bool:
    validations = context.get("runtime_validations") or []
    package_id = context.get("selected_package_id")
    runtime_target_id = context.get("selected_runtime_target_id")
    if not package_id or not runtime_target_id:
        return False

    return (
        _matching_runtime_validation(
            validations,
            package_id=package_id,
            runtime_target_id=runtime_target_id,
        )
        is not None
    )


def _proof_checklist(context: Dict[str, Any]) -> list[dict[str, str]]:
    latest_rollout = context.get("latest_rollout")
    validations = context.get("runtime_validations") or []
    benchmarks = context.get("benchmarks") or []
    package = context.get("selected_package")
    runtime = context.get("selected_runtime_target")
    device = context.get("selected_device")

    package_id = package.get("package_id") if isinstance(package, dict) else None
    runtime_target_id = (
        runtime.get("runtime_target_id") if isinstance(runtime, dict) else None
    )
    device_id = device.get("device_id") if isinstance(device, dict) else None
    package_validation = (
        package.get("metadata", {}).get("validation", {})
        if isinstance(package, dict) and isinstance(package.get("metadata"), dict)
        else {}
    )
    signature_verified = bool(package_validation.get("signature_verified"))
    strict_metadata = bool(package_validation.get("strict_metadata"))
    runtime_validation = _matching_runtime_validation(
        validations,
        package_id=package_id,
        runtime_target_id=runtime_target_id,
    )
    benchmark = _matching_benchmark(
        benchmarks,
        package_id=package_id,
        runtime_target_id=runtime_target_id,
        device_id=device_id,
    )
    rollout_state = (
        str(latest_rollout.get("state", "")) if isinstance(latest_rollout, dict) else ""
    )
    rollout_proven = rollout_state in {"activated", "rolled_back", "failed"}

    return [
        {
            "label": "Package trust",
            "state": "verified" if signature_verified and strict_metadata else "pending",
            "detail": (
                "signed package, strict metadata"
                if signature_verified and strict_metadata
                else "needs verified signature and strict metadata"
            ),
            "status": "ready" if signature_verified and strict_metadata else "waiting",
        },
        {
            "label": "Runtime validation",
            "state": "passed" if runtime_validation else "pending",
            "detail": (
                f"{runtime_validation.get('validation_id')}"
                if isinstance(runtime_validation, dict)
                else "run package in target image"
            ),
            "status": "ready" if runtime_validation else "waiting",
        },
        {
            "label": "Edge action",
            "state": rollout_state or "pending",
            "detail": (
                f"{latest_rollout.get('rollout_id')} on {latest_rollout.get('device_id')}"
                if isinstance(latest_rollout, dict)
                else "assign and apply rollout"
            ),
            "status": "ready" if rollout_proven else "waiting",
        },
        {
            "label": "Performance evidence",
            "state": "recorded" if benchmark else "pending",
            "detail": (
                f"{benchmark.get('benchmark_id')}"
                if isinstance(benchmark, dict)
                else "publish hardware-aware benchmark"
            ),
            "status": "ready" if benchmark else "waiting",
        },
    ]


def _matching_runtime_validation(
    validations: list[dict[str, Any]],
    *,
    package_id: str | None,
    runtime_target_id: str | None,
) -> dict[str, Any] | None:
    if not package_id or not runtime_target_id:
        return None
    for validation in validations:
        result = validation.get("result") if isinstance(validation, dict) else {}
        if not isinstance(result, dict):
            result = {}
        if (
            validation.get("package_id") == package_id
            and validation.get("runtime_target_id") == runtime_target_id
            and result.get("ok") is True
            and result.get("dry_run") is not True
        ):
            return validation
    return None


def _matching_benchmark(
    benchmarks: list[dict[str, Any]],
    *,
    package_id: str | None,
    runtime_target_id: str | None,
    device_id: str | None,
) -> dict[str, Any] | None:
    for benchmark in benchmarks:
        if package_id and benchmark.get("package_id") != package_id:
            continue
        if runtime_target_id and benchmark.get("runtime_target_id") != runtime_target_id:
            continue
        if device_id and benchmark.get("device_id") != device_id:
            continue
        return benchmark
    return None


def _find_by_id(items: list[dict[str, Any]], key: str, value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    return next((item for item in items if item.get(key) == value), None)


def _latest_rollout(rollouts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rollouts:
        return None
    return sorted(rollouts, key=lambda rollout: str(rollout.get("updated_at", "")))[-1]


def _latest_rollout_for_mission(
    rollouts: list[dict[str, Any]],
    *,
    package_id: str | None,
    runtime_target_id: str | None,
    device_id: str | None,
) -> dict[str, Any] | None:
    matches = []
    for rollout in rollouts:
        if package_id and rollout.get("package_id") != package_id:
            continue
        if runtime_target_id and rollout.get("runtime_target_id") != runtime_target_id:
            continue
        if device_id and rollout.get("device_id") != device_id:
            continue
        matches.append(rollout)
    return _latest_rollout(matches)


def _simulated_inventory_for_profile(profile: str) -> Dict[str, Any]:
    from temms.core.runtime_profiles import DEFAULT_RUNTIME_TARGETS, normalize_device_profile

    normalized = normalize_device_profile(profile) or profile
    runtime_target = next(
        (
            target
            for target in DEFAULT_RUNTIME_TARGETS.values()
            if normalized in target.get("device_profiles", [])
        ),
        {},
    )
    return {
        "schema_version": "temms-device-inventory/v1",
        "simulated": True,
        "device_profile": normalized,
        "os": runtime_target.get("os", "linux"),
        "arch": runtime_target.get("arch"),
        "runtimes": runtime_target.get("runtimes", {}),
        "accelerators": runtime_target.get("accelerators", {}),
    }


def _register_package_from_path(
    state,
    package_path: str,
    *,
    actor: str,
    strict_metadata: bool = True,
) -> Dict[str, Any]:
    from temms.core.package_archive import sign_package_artifact
    from temms.core.signing import validate_package

    hub = state.hub_lite
    path = Path(package_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Package not found: {package_path}")

    require_signature, signing_key = _ui_signature_policy(state)
    if require_signature:
        if not signing_key:
            raise ValueError("Package signing/verification requires a signing key")
        validation = validate_package(path, require_signature=True, signing_key=signing_key)
        if not validation.valid or not validation.signature_verified:
            sign_package_artifact(path, signing_key, signer="temms-hub-lite")

    return hub.upsert_package_from_source(
        path,
        actor=actor,
        require_signature=require_signature,
        signing_key=signing_key,
        strict_metadata=strict_metadata,
    )


def _default_package_output_dir(state) -> Path:
    if getattr(state, "daemon_config", None) is not None:
        return state.daemon_config.model_dir.parent / "packages"
    return state.model_cache.db_path.parent / "packages"


def _default_mlflow_tracking_uri(state) -> str:
    daemon_config = getattr(state, "daemon_config", None)
    configured = getattr(daemon_config, "mlflow_tracking_uri", None)
    return configured or "http://localhost:5000"


def _csv_list(value: str | None) -> list[str]:
    """Parse a comma-separated form field into a compact list."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _ui_success(message: str, *, refresh: bool = False) -> HTMLResponse:
    headers = {"HX-Refresh": "true"} if refresh else None
    return HTMLResponse(
        f'<div class="text-green-700 bg-green-50 rounded p-2">{_escape_html(message)}</div>',
        headers=headers,
    )


def _ui_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        f'<div class="text-red-700 bg-red-50 rounded p-2">{_escape_html(message)}</div>'
    )


def _ui_airgap_bundle_preview(bundle: dict[str, Any]) -> HTMLResponse:
    hub_lite = bundle.get("hub_lite") if isinstance(bundle.get("hub_lite"), dict) else {}
    artifacts = bundle.get("package_artifacts")
    counts = {
        "devices": len(hub_lite.get("devices", {}) if isinstance(hub_lite, dict) else {}),
        "packages": len(hub_lite.get("packages", {}) if isinstance(hub_lite, dict) else {}),
        "rollouts": len(hub_lite.get("rollouts", {}) if isinstance(hub_lite, dict) else {}),
        "runtime_targets": len(
            hub_lite.get("runtime_targets", {}) if isinstance(hub_lite, dict) else {}
        ),
        "artifacts": len(artifacts) if isinstance(artifacts, dict) else 0,
    }
    bundle_text = _escape_html(json.dumps(bundle, indent=2, sort_keys=True))
    summary = "".join(
        f'<div class="border border-zinc-800 bg-[#050606] p-3">'
        f'<div class="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">'
        f'{_escape_html(label.replace("_", " "))}</div>'
        f'<div class="mt-1 font-mono text-xl text-zinc-100">{value}</div>'
        f"</div>"
        for label, value in counts.items()
    )
    return HTMLResponse(
        f"""
        <div class="space-y-3 p-4">
            <div class="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                <div>
                    <h3 class="text-sm font-semibold uppercase tracking-[0.14em] text-zinc-100">Air-gap Bundle Ready</h3>
                    <p class="mt-1 text-xs text-zinc-500">Schema {_escape_html(str(bundle.get("schema_version", "-")))} exported at {_escape_html(str(bundle.get("exported_at", "-")))}</p>
                </div>
            </div>
            <div class="grid grid-cols-2 gap-2 md:grid-cols-5">{summary}</div>
            <details open class="border border-zinc-800 bg-[#050606]">
                <summary class="cursor-pointer px-3 py-2 text-xs font-semibold uppercase tracking-[0.12em] text-zinc-400">Bundle JSON</summary>
                <pre class="max-h-96 overflow-auto border-t border-zinc-800 p-3 text-xs text-zinc-300">{bundle_text}</pre>
            </details>
        </div>
        """
    )


def _ui_runtime_validation_result(
    result: dict[str, Any],
    *,
    signing_key: str | None = None,
    validation_record: dict[str, Any] | None = None,
) -> HTMLResponse:
    """Render a runtime validation result for the Hub operator console."""
    status = "Command preview ready" if result.get("dry_run") else "Validation passed"
    color_classes = "border-emerald-500/40 bg-emerald-500/10 text-emerald-700"
    if not result.get("ok"):
        status = "Validation failed"
        color_classes = "border-red-500/40 bg-red-500/10 text-red-700"

    command_text = _redact_secret(result.get("command_text") or "", signing_key)
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    exit_code = result.get("exit_code")
    exit_line = "" if exit_code is None else f"<div>Exit code: {_escape_html(str(exit_code))}</div>"
    validation_id = (validation_record or {}).get("validation_id")
    validation_line = (
        ""
        if not validation_id
        else f'<div class="mt-1 font-mono text-xs">Evidence: {_escape_html(str(validation_id))}</div>'
    )
    stdout_block = (
        ""
        if not stdout
        else (
            '<div class="mt-3"><div class="text-[10px] font-semibold uppercase tracking-[0.14em]">'
            'stdout</div><pre class="mt-1 overflow-auto border border-zinc-700 p-2 text-xs">'
            f"{_escape_html(stdout)}</pre></div>"
        )
    )
    stderr_block = (
        ""
        if not stderr
        else (
            '<div class="mt-3"><div class="text-[10px] font-semibold uppercase tracking-[0.14em]">'
            'stderr</div><pre class="mt-1 overflow-auto border border-zinc-700 p-2 text-xs">'
            f"{_escape_html(stderr)}</pre></div>"
        )
    )

    html = f"""
    <div class="border {color_classes} p-3 text-sm">
        <div class="font-semibold">{_escape_html(status)}</div>
        {validation_line}
        <div class="mt-1 font-mono text-xs">
            Target: {_escape_html(result.get("runtime_target_id") or "")}
            <span class="mx-1">/</span>
            Image: {_escape_html(result.get("image") or "")}
        </div>
        {exit_line}
        <div class="mt-3">
            <div class="text-[10px] font-semibold uppercase tracking-[0.14em]">Docker command</div>
            <pre class="mt-1 overflow-auto border border-zinc-700 p-2 text-xs">{_escape_html(command_text)}</pre>
        </div>
        {stdout_block}
        {stderr_block}
    </div>
    """
    return HTMLResponse(html)


def _ui_compatibility_preview_result(result: dict[str, Any]) -> HTMLResponse:
    """Render a rollout compatibility preview for the Hub operator console."""
    compatible = bool(result.get("compatible"))
    status = "Compatibility clear" if compatible else "Compatibility blocked"
    color_classes = (
        "border-emerald-500/40 bg-emerald-500/10 text-emerald-700"
        if compatible
        else "border-red-500/40 bg-red-500/10 text-red-700"
    )
    device = result.get("device") if isinstance(result.get("device"), dict) else {}
    package = result.get("package") if isinstance(result.get("package"), dict) else {}
    runtime_target = (
        result.get("runtime_target") if isinstance(result.get("runtime_target"), dict) else {}
    )
    runtime_label = (
        f"{runtime_target.get('runtime_target_id')} ({runtime_target.get('image')})"
        if runtime_target
        else "auto / device inventory"
    )
    failures = result.get("failures") if isinstance(result.get("failures"), list) else []
    failure_block = ""
    if failures:
        items = "".join(f"<li>{_escape_html(str(failure))}</li>" for failure in failures)
        failure_block = f'<ul class="mt-2 list-disc pl-5 text-xs">{items}</ul>'

    html = f"""
    <div class="border {color_classes} p-3 text-sm">
        <div class="font-semibold">{_escape_html(status)}</div>
        <div class="mt-2 grid gap-1 font-mono text-xs">
            <div>Device: {_escape_html(str(device.get("device_id") or ""))} ({_escape_html(str(device.get("profile") or "unknown"))})</div>
            <div>Package: {_escape_html(str(package.get("package_id") or ""))} v{_escape_html(str(package.get("version") or ""))}</div>
            <div>Runtime: {_escape_html(runtime_label)}</div>
        </div>
        {failure_block}
    </div>
    """
    return HTMLResponse(html)


def _ui_evidence_preview(bundle: dict[str, Any]) -> HTMLResponse:
    """Render a mission-readable evidence summary with raw JSON available."""
    hub_lite = bundle.get("hub_lite") if isinstance(bundle.get("hub_lite"), dict) else {}
    devices = hub_lite.get("devices") if isinstance(hub_lite.get("devices"), dict) else {}
    catalog_packages = hub_lite.get("packages") if isinstance(hub_lite.get("packages"), dict) else {}
    rollouts = hub_lite.get("rollouts") if isinstance(hub_lite.get("rollouts"), dict) else {}
    decisions = bundle.get("decisions") if isinstance(bundle.get("decisions"), list) else []
    timeline = bundle.get("timeline") if isinstance(bundle.get("timeline"), list) else []
    runtime_validations = (
        bundle.get("runtime_validations")
        if isinstance(bundle.get("runtime_validations"), list)
        else []
    )
    benchmarks = bundle.get("hub_benchmarks") if isinstance(bundle.get("hub_benchmarks"), list) else []
    package_imports = (
        bundle.get("package_imports") if isinstance(bundle.get("package_imports"), list) else []
    )
    telemetry = bundle.get("telemetry") if isinstance(bundle.get("telemetry"), dict) else {}
    diagnostics = bundle.get("diagnostics") if isinstance(bundle.get("diagnostics"), dict) else {}
    model_cache = (
        diagnostics.get("model_cache") if isinstance(diagnostics.get("model_cache"), dict) else {}
    )
    cache_health = (
        model_cache.get("health") if isinstance(model_cache.get("health"), dict) else {}
    )

    cards = [
        ("Devices", len(devices)),
        ("Packages", len(catalog_packages) or len(package_imports)),
        ("Rollouts", len(rollouts)),
        ("Decisions", len(decisions)),
        ("Runtime checks", len(runtime_validations)),
        ("Benchmarks", len(benchmarks)),
        ("Telemetry", telemetry.get("count", 0)),
        ("Cache", cache_health.get("status") or "unknown"),
    ]
    card_html = "".join(_evidence_metric_card(label, value) for label, value in cards)

    trust_rows = "".join(
        _evidence_package_row(package)
        for package in (list(catalog_packages.values()) or package_imports)
        if isinstance(package, dict)
    )
    if not trust_rows:
        trust_rows = (
            '<tr><td colspan="5" class="px-3 py-3 text-center text-xs text-zinc-500">'
            "No package trust evidence recorded</td></tr>"
        )

    decision_cards = "".join(
        _evidence_decision_card(decision) for decision in decisions[:5] if isinstance(decision, dict)
    )
    if not decision_cards:
        decision_cards = (
            '<div class="border border-zinc-800 bg-[#0d0f0f] p-3 text-xs text-zinc-500">'
            "No model decisions recorded</div>"
        )

    timeline_rows = "".join(
        _evidence_timeline_row(entry) for entry in timeline[:10] if isinstance(entry, dict)
    )
    if not timeline_rows:
        timeline_rows = (
            '<tr><td colspan="4" class="px-3 py-3 text-center text-xs text-zinc-500">'
            "No timeline entries recorded</td></tr>"
        )

    raw_json = _escape_html(json.dumps(bundle, indent=2, sort_keys=True))
    exported_at = _escape_html(str(bundle.get("exported_at") or ""))
    schema = _escape_html(str(bundle.get("schema_version") or ""))
    html = f"""
    <div class="space-y-4 p-4">
        <div class="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div>
                <div class="text-[10px] font-semibold uppercase tracking-[0.14em] text-emerald-300">Mission Evidence</div>
                <div class="mt-1 text-sm font-semibold text-zinc-100">{schema}</div>
                <div class="mt-1 font-mono text-xs text-zinc-500">{exported_at}</div>
            </div>
            <div class="font-mono text-xs text-zinc-500">
                Timeline: {_escape_html(str(len(timeline)))} events
            </div>
        </div>

        <div class="grid grid-cols-2 gap-px bg-zinc-800 md:grid-cols-4">
            {card_html}
        </div>

        <section class="border border-zinc-800 bg-[#090a0a]">
            <div class="border-b border-zinc-800 px-3 py-2">
                <h3 class="text-xs font-semibold uppercase tracking-[0.14em] text-zinc-100">Package Trust Posture</h3>
            </div>
            <div class="overflow-x-auto">
                <table class="min-w-full">
                    <thead class="bg-[#0d0f0f]">
                        <tr>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Package</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Version</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Signature</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Strict Metadata</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Profiles</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-zinc-800">{trust_rows}</tbody>
                </table>
            </div>
        </section>

        <section class="border border-zinc-800 bg-[#090a0a]">
            <div class="border-b border-zinc-800 px-3 py-2">
                <h3 class="text-xs font-semibold uppercase tracking-[0.14em] text-zinc-100">Why Models Switched</h3>
            </div>
            <div class="grid gap-3 p-3 lg:grid-cols-2">{decision_cards}</div>
        </section>

        <section class="border border-zinc-800 bg-[#090a0a]">
            <div class="border-b border-zinc-800 px-3 py-2">
                <h3 class="text-xs font-semibold uppercase tracking-[0.14em] text-zinc-100">Mission Timeline</h3>
            </div>
            <div class="overflow-x-auto">
                <table class="min-w-full">
                    <thead class="bg-[#0d0f0f]">
                        <tr>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Time</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Kind</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Slot</th>
                            <th class="px-3 py-2 text-left text-[10px] uppercase tracking-[0.14em] text-zinc-500">Summary</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-zinc-800">{timeline_rows}</tbody>
                </table>
            </div>
        </section>

        <details class="border border-zinc-800 bg-[#090a0a]">
            <summary class="cursor-pointer px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-zinc-300">
                Raw Evidence JSON
            </summary>
            <pre class="max-h-96 overflow-auto border-t border-zinc-800 bg-[#050606] p-3 text-xs text-zinc-200">{raw_json}</pre>
        </details>
    </div>
    """
    return HTMLResponse(html)


def _evidence_metric_card(label: str, value: Any) -> str:
    return (
        '<div class="bg-[#0d0f0f] p-3">'
        f'<div class="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">{_escape_html(label)}</div>'
        f'<div class="mt-1 font-mono text-lg font-semibold text-zinc-100">{_escape_html(str(value))}</div>'
        "</div>"
    )


def _evidence_package_row(package: dict[str, Any]) -> str:
    metadata = package.get("metadata") if isinstance(package.get("metadata"), dict) else {}
    validation = metadata.get("validation") if isinstance(metadata.get("validation"), dict) else {}
    signature_verified = bool(
        package.get("signature_verified")
        or validation.get("signature_verified")
        or metadata.get("signature_verified")
    )
    strict_metadata = bool(validation.get("strict_metadata") or metadata.get("strict_metadata"))
    profiles = package.get("device_profiles") or []
    if isinstance(profiles, list):
        profile_text = ", ".join(str(profile) for profile in profiles)
    else:
        profile_text = str(profiles or "")
    package_id = package.get("package_id") or package.get("id") or "unknown"
    return (
        "<tr>"
        f'<td class="px-3 py-2 font-mono text-xs text-zinc-100">{_escape_html(str(package_id))}</td>'
        f'<td class="px-3 py-2 font-mono text-xs text-zinc-300">{_escape_html(str(package.get("version") or ""))}</td>'
        f'<td class="px-3 py-2">{_evidence_status_badge(signature_verified, "verified", "missing")}</td>'
        f'<td class="px-3 py-2">{_evidence_status_badge(strict_metadata, "strict", "lab")}</td>'
        f'<td class="px-3 py-2 font-mono text-xs text-zinc-400">{_escape_html(profile_text or "-")}</td>'
        "</tr>"
    )


def _evidence_decision_card(decision: dict[str, Any]) -> str:
    audit = decision.get("audit_metadata") if isinstance(decision.get("audit_metadata"), dict) else {}
    evaluation = (
        audit.get("policy_evaluation") if isinstance(audit.get("policy_evaluation"), dict) else {}
    )
    matched_rule = (
        evaluation.get("matched_rule") if isinstance(evaluation.get("matched_rule"), dict) else {}
    )
    rule_label = _decision_rule_label(decision, evaluation, matched_rule)
    condition_lines = _evidence_condition_lines(matched_rule)
    package_id = audit.get("package_id") or (audit.get("package") or {}).get("package_id")
    provenance = audit.get("provenance") if isinstance(audit.get("provenance"), dict) else {}
    run_id = provenance.get("run_id")
    package_line = ""
    if package_id or run_id:
        package_line = (
            '<div class="mt-2 font-mono text-xs text-zinc-500">'
            f"Package: {_escape_html(str(package_id or '-'))}"
            f" / Run: {_escape_html(str(run_id or '-'))}</div>"
        )
    return f"""
    <article class="border border-zinc-800 bg-[#0d0f0f] p-3">
        <div class="flex items-start justify-between gap-3">
            <div>
                <div class="font-mono text-xs text-zinc-500">{_escape_html(str(decision.get("created_at") or ""))}</div>
                <div class="mt-1 text-sm font-semibold text-zinc-100">
                    {_escape_html(str(decision.get("from_model") or "none"))}
                    <span class="text-zinc-500">-&gt;</span>
                    {_escape_html(str(decision.get("to_model") or ""))}
                </div>
            </div>
            {_evidence_badge(str(decision.get("trigger_type") or "decision"), "cyan")}
        </div>
        <div class="mt-2 text-xs text-zinc-300">{_escape_html(rule_label)}</div>
        {condition_lines}
        {package_line}
    </article>
    """


def _decision_rule_label(
    decision: dict[str, Any],
    evaluation: dict[str, Any],
    matched_rule: dict[str, Any],
) -> str:
    if matched_rule:
        return (
            f"{matched_rule.get('policy') or 'policy'} / {matched_rule.get('rule') or 'rule'} "
            f"priority {matched_rule.get('priority', '-')}"
        )
    if evaluation:
        return str(evaluation.get("reason") or decision.get("trigger_detail") or "policy evaluation")
    return str(decision.get("trigger_detail") or "manual or system decision")


def _evidence_condition_lines(matched_rule: dict[str, Any]) -> str:
    conditions = (
        matched_rule.get("conditions") if isinstance(matched_rule.get("conditions"), dict) else {}
    )
    items = conditions.get("items") if isinstance(conditions.get("items"), list) else []
    if not items:
        return ""
    rows = []
    for item in items[:4]:
        if not isinstance(item, dict):
            continue
        actual = item.get("actual", item.get("reason", "missing"))
        confidence = item.get("confidence")
        confidence_text = "" if confidence is None else f" conf={confidence}"
        rows.append(
            '<li class="font-mono text-xs text-zinc-400">'
            f'{_escape_html(str(item.get("metric") or ""))} '
            f'{_escape_html(str(item.get("operator") or ""))} '
            f'{_escape_html(str(item.get("expected") or ""))} '
            f'= {_escape_html(str(actual))}{_escape_html(confidence_text)} '
            f'{_evidence_status_badge(bool(item.get("matched")), "matched", "miss")}'
            "</li>"
        )
    if not rows:
        return ""
    return '<ul class="mt-2 space-y-1">' + "".join(rows) + "</ul>"


def _evidence_timeline_row(entry: dict[str, Any]) -> str:
    return (
        "<tr>"
        f'<td class="px-3 py-2 font-mono text-xs text-zinc-500">{_escape_html(str(entry.get("timestamp") or ""))}</td>'
        f'<td class="px-3 py-2">{_evidence_badge(str(entry.get("kind") or ""), "zinc")}</td>'
        f'<td class="px-3 py-2 font-mono text-xs text-cyan-300">{_escape_html(str(entry.get("slot") or "-"))}</td>'
        f'<td class="px-3 py-2 text-xs text-zinc-300">{_escape_html(str(entry.get("summary") or ""))}</td>'
        "</tr>"
    )


def _evidence_status_badge(ok: bool, ok_label: str, fail_label: str) -> str:
    return _evidence_badge(ok_label if ok else fail_label, "emerald" if ok else "amber")


def _evidence_badge(label: str, tone: str) -> str:
    classes = {
        "emerald": "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
        "amber": "border-amber-400/40 bg-amber-400/10 text-amber-200",
        "cyan": "border-cyan-400/40 bg-cyan-400/10 text-cyan-200",
        "zinc": "border-zinc-700 bg-zinc-950 text-zinc-300",
    }.get(tone, "border-zinc-700 bg-zinc-950 text-zinc-300")
    return (
        f'<span class="inline-flex border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] {classes}">'
        f"{_escape_html(label)}</span>"
    )


def _redact_secret(value: str, secret: str | None) -> str:
    if not secret:
        return value
    return value.replace(secret, "********")


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
