"""
TEMMS Web UI routes using FastAPI + Jinja2 + HTMX.

Provides a lightweight dashboard for monitoring and controlling the TEMMS daemon.
No Node.js or frontend build step required — HTMX and Tailwind CSS loaded via CDN.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

# Templates directory (relative to this file)
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_ui_router(get_state_func) -> APIRouter:
    """
    Create the UI router with state dependency injection.

    Args:
        get_state_func: Dependency function that returns AppState

    Returns:
        Configured APIRouter
    """
    router = APIRouter(prefix="/ui", tags=["ui"])

    def _hub_cache_dir(state) -> Path:
        """Resolve the Hub workspace used for imported package staging."""
        if state.daemon_config is not None and getattr(
            state.daemon_config,
            "model_dir",
            None,
        ):
            return Path(state.daemon_config.model_dir).parent / "cache"
        return Path(state.model_storage.model_dir).parent / "cache"

    def _safe_slug(value: str) -> str:
        """Create a filesystem-friendly name for staged registry packages."""
        slug = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "-"
            for char in value
        ).strip("-")
        return slug or "model"

    def _http_registry_base(value: Any) -> str:
        """Return a clickable registry base URL when the source is browser-openable."""
        if not value:
            return ""
        text = str(value).strip().rstrip("/")
        if text.startswith(("http://", "https://")):
            return text
        return ""

    def _mlflow_model_url(base_url: str, model_name: str, version: str) -> str:
        """Build the MLflow registered model version URL."""
        if not base_url or not model_name or not version:
            return ""
        model_path = quote(model_name, safe="")
        version_path = quote(str(version), safe="")
        return f"{base_url}/#/models/{model_path}/versions/{version_path}"

    def _mlflow_run_url(base_url: str, experiment_id: str, run_id: str) -> str:
        """Build the MLflow run URL when experiment metadata is available."""
        if not base_url or not experiment_id or not run_id:
            return ""
        experiment_path = quote(str(experiment_id), safe="")
        run_path = quote(str(run_id), safe="")
        return f"{base_url}/#/experiments/{experiment_path}/runs/{run_path}"

    def _annotate_registry_models(registry_models, cached_models):
        """Mark registry versions that are already imported in the local cache."""
        cached_by_key = {
            (model.name, str(model.version)): model
            for model in cached_models
        }
        annotated = []

        for registry_model in registry_models:
            item = dict(registry_model)
            versions = item.get("versions") or item.get("latest_versions") or []
            annotated_versions = []

            for raw_version in versions:
                version = dict(raw_version)
                version_number = str(version.get("version", ""))
                cached_model = cached_by_key.get((item.get("name", ""), version_number))
                version["version"] = version_number
                version["imported"] = cached_model is not None
                version["cached_model_id"] = cached_model.id if cached_model else None
                version["format"] = version.get("format") or "-"
                annotated_versions.append(version)

            item["versions"] = annotated_versions
            annotated.append(item)

        return annotated

    def _condition_values(raw_conditions):
        """Normalize condition store output for templates."""
        if isinstance(raw_conditions, dict):
            conditions = list(raw_conditions.values())
        else:
            conditions = list(raw_conditions)

        rows = []
        for condition in conditions:
            if hasattr(condition, "to_dict"):
                rows.append(condition.to_dict())
            else:
                rows.append(condition)
        return rows

    def _truthy_evidence(value: Any) -> bool:
        """Interpret loose package/registry metadata as readiness evidence."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value > 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "0", "false", "failed", "fail", "no", "none", "unknown"}:
                return False
            return True
        return bool(value)

    def _has_named_evidence(*sources: dict[str, Any], keys: set[str]) -> bool:
        """Find evidence in a small set of known metadata keys."""
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                normalized_key = str(key).strip().lower().replace("-", "_")
                if normalized_key in keys and _truthy_evidence(value):
                    return True
                if isinstance(value, dict) and _has_named_evidence(value, keys=keys):
                    return True
        return False

    def _first_evidence_record(*sources: dict[str, Any], keys: set[str]) -> dict[str, Any]:
        """Find a structured readiness evidence record in nested metadata."""
        normalized_keys = {
            key.strip().lower().replace("-", "_")
            for key in keys
        }
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                normalized_key = str(key).strip().lower().replace("-", "_")
                if normalized_key in normalized_keys and isinstance(value, dict):
                    return value
                if isinstance(value, dict):
                    nested = _first_evidence_record(value, keys=keys)
                    if nested:
                        return nested
        return {}

    def _evidence_detail(default: str, evidence: dict[str, Any]) -> str:
        """Create a compact tooltip from a structured evidence record."""
        if not evidence:
            return default

        parts = []
        detail = evidence.get("detail")
        source = evidence.get("source")
        run_id = evidence.get("run_id")
        if detail:
            parts.append(str(detail))
        if source:
            parts.append(f"source: {source}")
        if run_id:
            parts.append(f"run: {run_id}")
        if evidence.get("protected_by_signature") is True:
            parts.append("signed")
        elif evidence.get("passed") is True:
            parts.append("unsigned")

        return "; ".join(parts) or default

    def _check(label: str, state: str, detail: str) -> dict[str, str]:
        return {"label": label, "state": state, "detail": detail}

    def _hub_refresh_headers() -> dict[str, str]:
        """Tell HTMX actions to return operators to the updated Hub catalog."""
        return {"HX-Redirect": "/ui/models"}

    def _required_deploy_evidence() -> list[str]:
        """Evidence required before Hub allows an operator deploy."""
        raw_value = os.environ.get(
            "TEMMS_HUB_REQUIRED_EVIDENCE",
            "signed,sim,test,val",
        )
        label_map = {
            "signed": "Signed",
            "signature": "Signed",
            "sim": "Sim",
            "simulation": "Sim",
            "test": "Test",
            "tests": "Test",
            "val": "Val",
            "validation": "Val",
            "hash": "Val",
        }
        labels = []
        for item in raw_value.split(","):
            label = label_map.get(item.strip().lower())
            if label and label not in labels:
                labels.append(label)
        return labels or ["Val"]

    def _deployment_gate(trust_checks: list[dict[str, str]]) -> dict[str, Any]:
        """Convert evidence chips into a deploy/no-deploy decision."""
        required = _required_deploy_evidence()
        passed = {
            check["label"]
            for check in trust_checks
            if check["state"] == "pass"
        }
        missing_required = [
            label
            for label in required
            if label not in passed
        ]
        missing_advisory = [
            label
            for label in ["Signed", "Sim", "Test", "Val"]
            if label not in passed and label not in missing_required
        ]

        if missing_required:
            label = "Evidence needed"
            detail = "Missing required evidence: " + ", ".join(missing_required)
        elif missing_advisory:
            label = "Deploy ready"
            detail = "Advisory evidence missing: " + ", ".join(missing_advisory)
        else:
            label = "Deploy ready"
            detail = "All readiness evidence present"

        return {
            "allowed": not missing_required,
            "label": label,
            "detail": detail,
            "required": required,
            "missing_required": missing_required,
            "missing_advisory": missing_advisory,
        }

    def _package_for_model(model, packages_by_id):
        if model is None:
            return None
        return packages_by_id.get(getattr(model, "package_id", ""))

    def _model_format(model) -> str:
        if model is None:
            return "-"
        model_format = getattr(model, "format", "")
        return getattr(model_format, "value", model_format) or "-"

    def _policy_candidates_by_slot(policies) -> dict[str, set[str]]:
        """Collect model names implied by slot policies."""
        candidates_by_slot: dict[str, set[str]] = {}
        for policy in policies:
            spec = getattr(policy, "spec", None)
            if spec is None:
                continue

            slot_name = getattr(spec, "slot", None)
            if not slot_name:
                continue

            candidates = candidates_by_slot.setdefault(slot_name, set())
            default_model = getattr(spec, "default_model", None)
            if default_model:
                candidates.add(default_model)
            candidates.update(getattr(spec, "fallback_chain", []) or [])

            for rule in getattr(spec, "rules", []) or []:
                action = getattr(rule, "action", None)
                if action is None:
                    continue
                switch_to = getattr(action, "switch_to", None)
                if switch_to:
                    candidates.add(switch_to)
                candidates.update(getattr(action, "preload", []) or [])

        return candidates_by_slot

    def _slot_deployment_options(model, slots, policy_candidates_by_slot):
        if model is None:
            return [], []

        model_keys = {model.name, model.id}
        compatible = []
        blocked = []

        for slot in slots:
            declared = set(getattr(slot, "candidates", []) or [])
            metadata = getattr(slot, "metadata", {}) or {}
            declared.update(metadata.get("fallback_chain", []) or [])
            declared.update(policy_candidates_by_slot.get(slot.name, set()))

            slot_row = {
                "name": slot.name,
                "description": slot.description,
                "state": slot.state.value,
                "required": slot.required,
                "active_model_id": slot.active_model_id,
                "is_active": slot.active_model_id == model.id,
                "declared": sorted(declared),
            }

            if not declared or model_keys & declared:
                compatible.append(slot_row)
            else:
                blocked.append(slot_row)

        return compatible, blocked

    def _model_trust_checks(model=None, package=None, registry_version=None):
        """Build signed/sim/test/validation evidence for a catalog row."""
        registry_version = registry_version or {}
        metadata = getattr(model, "metadata", {}) if model is not None else {}
        manifest = getattr(package, "manifest", {}) if package is not None else {}
        validation = manifest.get("validation", {}) if isinstance(manifest, dict) else {}
        registry_validation = registry_version.get("validation", {}) or {}
        provenance = manifest.get("provenance", {}) if isinstance(manifest, dict) else {}
        tags = registry_version.get("tags", {}) or {}
        metrics = registry_version.get("metrics", {}) or {}
        sim_evidence = _first_evidence_record(
            metadata,
            validation,
            registry_validation,
            keys={"sim_evidence", "simulation_evidence"},
        )
        test_evidence = _first_evidence_record(
            metadata,
            validation,
            registry_validation,
            keys={"test_evidence", "tests_evidence"},
        )

        signature_verified = _has_named_evidence(
            manifest,
            validation,
            provenance,
            keys={"signature_verified", "signature_verification_passed"},
        )
        signature_present = _has_named_evidence(
            metadata,
            manifest,
            validation,
            registry_validation,
            provenance,
            tags,
            keys={"signature", "signatures", "signed", "signature_present"},
        )
        sim_passed = _has_named_evidence(
            metadata,
            manifest,
            validation,
            registry_validation,
            tags,
            keys={"sim", "simulation", "sim_passed", "sim_verified"},
        ) or any("sim" in key.lower() for key in metrics)
        tests_passed = _has_named_evidence(
            metadata,
            manifest,
            validation,
            registry_validation,
            tags,
            keys={"test", "tests", "tested", "tests_passed", "test_status"},
        ) or any(key.lower() not in {"size_bytes"} for key in metrics)
        hash_verified = _has_named_evidence(
            metadata,
            validation,
            registry_validation,
            keys={"hash_verified"},
        )
        validated = model is not None and bool(hash_verified)

        imported = model is not None
        return [
            _check(
                "Signed",
                "pass" if signature_verified
                else ("warn" if imported or signature_present else "unknown"),
                "Signature verified" if signature_verified
                else ("Signature unverified" if signature_present else "Needs signature"),
            ),
            _check(
                "Sim",
                "pass" if sim_passed else ("warn" if imported else "unknown"),
                _evidence_detail(
                    "Sim passed" if sim_passed else ("Needs sim" if imported else "Unknown"),
                    sim_evidence,
                ),
            ),
            _check(
                "Test",
                "pass" if tests_passed else ("warn" if imported else "unknown"),
                _evidence_detail(
                    "Tests passed" if tests_passed else ("Needs test" if imported else "Unknown"),
                    test_evidence,
                ),
            ),
            _check("Val", "pass" if validated else "unknown",
                   "Hash verified" if validated else "Validate on import"),
        ]

    def _catalog_row(
        *,
        name: str,
        version: str,
        source: str,
        model=None,
        package=None,
        registry_model_name: str = "",
        registry_version: dict[str, Any] | None = None,
        registry_tracking_uri: str = "",
        slots=None,
        policy_candidates_by_slot=None,
    ) -> dict[str, Any]:
        registry_version = registry_version or {}
        slots = slots or []
        policy_candidates_by_slot = policy_candidates_by_slot or {}
        slot_options, blocked_slots = _slot_deployment_options(
            model,
            slots,
            policy_candidates_by_slot,
        )
        deployed_slots = [
            option["name"]
            for option in slot_options
            if option["is_active"]
        ]

        trust_checks = _model_trust_checks(model, package, registry_version)
        deploy_gate = _deployment_gate(trust_checks)

        if deployed_slots:
            state_label = "Deployed"
            state_tone = "green"
            state_rank = 0
        elif model is not None and deploy_gate["allowed"]:
            state_label = "Deploy ready"
            state_tone = "blue"
            state_rank = 1
        elif model is not None:
            state_label = "Needs evidence"
            state_tone = "amber"
            state_rank = 2
        else:
            state_label = "Registry only"
            state_tone = "slate"
            state_rank = 3

        row_format = registry_version.get("format") or _model_format(model)
        metadata = getattr(model, "metadata", {}) if model is not None else {}
        if not isinstance(metadata, dict):
            metadata = {}
        manifest = getattr(package, "manifest", {}) if package is not None else {}
        if not isinstance(manifest, dict):
            manifest = {}
        mlflow_metadata = metadata.get("mlflow", {})
        if not isinstance(mlflow_metadata, dict):
            mlflow_metadata = {}

        registry_source = (
            registry_version.get("tracking_uri")
            or mlflow_metadata.get("tracking_uri")
            or manifest.get("source_registry")
            or registry_tracking_uri
            or ""
        )
        registry_base_url = (
            _http_registry_base(registry_source)
            or _http_registry_base(registry_tracking_uri)
        )
        registry_model = (
            registry_model_name
            or mlflow_metadata.get("model_name")
            or name
        )
        registry_version_id = (
            registry_version.get("version")
            or mlflow_metadata.get("model_version")
            or version
        )
        run_id = (
            registry_version.get("run_id")
            or mlflow_metadata.get("run_id")
            or manifest.get("mlflow_run_id")
            or ""
        )
        experiment_id = (
            registry_version.get("experiment_id")
            or mlflow_metadata.get("experiment_id")
            or manifest.get("mlflow_experiment_id")
            or ""
        )
        package_id = getattr(model, "package_id", "") if model is not None else ""

        return {
            "key": f"{source}:{name}:{version}:{getattr(model, 'id', '')}",
            "name": name,
            "version": version,
            "format": row_format or "-",
            "source": source,
            "state_label": state_label,
            "state_tone": state_tone,
            "state_rank": state_rank,
            "model_id": getattr(model, "id", ""),
            "package_id": package_id,
            "package_name": getattr(package, "name", ""),
            "registry_model_name": registry_model_name,
            "registry_version": registry_version.get("version", ""),
            "stage": registry_version.get("stage") or registry_version.get("status") or "",
            "aliases": registry_version.get("aliases", []) or [],
            "run_id": run_id,
            "run_label": run_id[:12] if run_id else "",
            "experiment_id": experiment_id,
            "registry_source": registry_source,
            "registry_source_url": registry_base_url,
            "registry_link_source": registry_base_url or registry_source,
            "registry_model_url": _mlflow_model_url(
                registry_base_url,
                registry_model,
                str(registry_version_id),
            ),
            "registry_run_url": _mlflow_run_url(
                registry_base_url,
                str(experiment_id),
                str(run_id),
            ),
            "metrics_count": len(registry_version.get("metrics", {}) or {}),
            "imported_at": getattr(model, "imported_at", None),
            "deployed_slots": deployed_slots,
            "slot_options": slot_options,
            "blocked_slots": blocked_slots,
            "trust_checks": trust_checks,
            "deploy_gate": deploy_gate,
        }

    def _hub_catalog(
        registry_models,
        cached_models,
        packages,
        slots,
        policies,
        registry_tracking_uri: str = "",
    ):
        packages_by_id = {package.id: package for package in packages}
        cached_by_key = {
            (model.name, str(model.version)): model
            for model in cached_models
        }
        included_model_ids = set()
        policy_candidates = _policy_candidates_by_slot(policies)
        rows = []

        for registry_model in _annotate_registry_models(registry_models, cached_models):
            for version in registry_model.get("versions", []):
                cached_model = cached_by_key.get(
                    (registry_model.get("name", ""), str(version.get("version", "")))
                )
                package = _package_for_model(cached_model, packages_by_id)
                if cached_model is not None:
                    included_model_ids.add(cached_model.id)

                rows.append(_catalog_row(
                    name=registry_model.get("name", ""),
                    version=str(version.get("version", "")),
                    source="MLflow Registry",
                    model=cached_model,
                    package=package,
                    registry_model_name=registry_model.get("name", ""),
                    registry_version=version,
                    registry_tracking_uri=registry_tracking_uri,
                    slots=slots,
                    policy_candidates_by_slot=policy_candidates,
                ))

        for model in cached_models:
            if model.id in included_model_ids:
                continue
            rows.append(_catalog_row(
                name=model.name,
                version=str(model.version),
                source="Package Import",
                model=model,
                package=_package_for_model(model, packages_by_id),
                registry_tracking_uri=registry_tracking_uri,
                slots=slots,
                policy_candidates_by_slot=policy_candidates,
            ))

        rows.sort(key=lambda row: (
            row["state_rank"],
            row["name"].lower(),
            row["version"],
        ))
        return rows

    def _catalog_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "total": len(rows),
            "registry_only": sum(row["state_label"] == "Registry only" for row in rows),
            "needs_evidence": sum(row["state_label"] == "Needs evidence" for row in rows),
            "ready": sum(row["state_label"] == "Deploy ready" for row in rows),
            "deployed": sum(row["state_label"] == "Deployed" for row in rows),
        }

    def _hub_models_context(request: Request, state) -> dict[str, Any]:
        """Build the single Hub model inventory context."""
        cached_models = state.model_cache.list_models()
        packages = state.model_cache.list_packages()
        slots = state.slot_manager.list_slots()
        policies = state.policy_engine.list_policies()
        registry_models = []
        registry_status = {
            "available": False,
            "tracking_uri": "",
            "message": "",
            "detail": "",
        }

        try:
            from temms.mlflow_bridge import MLflowBridge
            bridge = MLflowBridge()
            registry_status["available"] = bridge.available
            registry_status["tracking_uri"] = bridge.tracking_uri

            if bridge.available:
                registry_models = bridge.list_models()
                if getattr(bridge, "last_error", ""):
                    registry_status["available"] = False
                    registry_status["message"] = "Registry unavailable"
                    registry_status["detail"] = bridge.last_error
                elif not registry_models:
                    registry_status["message"] = "No registry models found."
            else:
                registry_status["message"] = "MLflow integration is not installed."
        except Exception as e:
            logger.warning("Could not load MLflow registry models: %s", e)
            registry_status["message"] = f"Registry unavailable: {e}"

        catalog_models = _hub_catalog(
            registry_models,
            cached_models,
            packages,
            slots,
            policies,
            registry_status["tracking_uri"],
        )

        return {
            "request": request,
            "models": cached_models,
            "catalog_models": catalog_models,
            "catalog_summary": _catalog_summary(catalog_models),
            "required_evidence": _required_deploy_evidence(),
            "registry_models": _annotate_registry_models(registry_models, cached_models),
            "registry_status": registry_status,
            "packages": packages,
            "slots": slots,
        }

    # ---- Dashboard ----

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, state=Depends(get_state_func)):
        """Main dashboard with system overview."""
        slots = state.slot_manager.list_slots()
        conditions = _condition_values(state.condition_store.get_all())
        policies = state.policy_engine.list_policies()
        uptime = time.time() - state.start_time

        # Determine system health
        has_error = any(s.state.value == "error" for s in slots)
        has_degraded = any(
            s.state.value != "running" and s.required for s in slots
        )
        inactive_slots = [
            slot
            for slot in slots
            if not slot.active_model_id or slot.state.value != "running"
        ]

        if has_error:
            health = "error"
        elif has_degraded or inactive_slots:
            health = "degraded"
        else:
            health = "healthy"

        packages_by_id = {
            package.id: package
            for package in state.model_cache.list_packages()
        }
        cached_models = state.model_cache.list_models()
        ready_models = 0
        needs_evidence_models = 0
        for model in cached_models:
            package = _package_for_model(model, packages_by_id)
            trust_checks = _model_trust_checks(model, package)
            if _deployment_gate(trust_checks)["allowed"]:
                ready_models += 1
            else:
                needs_evidence_models += 1

        # Get recent decisions
        decisions = []
        for slot in slots:
            slot_decisions = state.slot_manager.get_decision_log(
                slot.name, limit=5
            )
            decisions.extend(slot_decisions)

        # Sort decisions by timestamp (newest first)
        decisions.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        decisions = decisions[:10]

        return templates.TemplateResponse(request, "dashboard.html", {
            "health": health,
            "slots": slots,
            "conditions": conditions[:10],
            "conditions_count": len(conditions),
            "policies": policies,
            "decisions": decisions,
            "uptime": uptime,
            "active_slots_count": len(slots) - len(inactive_slots),
            "inactive_slots_count": len(inactive_slots),
            "models_count": len(cached_models),
            "ready_models_count": ready_models,
            "needs_evidence_models_count": needs_evidence_models,
        })

    # ---- Slots ----

    @router.get("/slots", response_class=HTMLResponse)
    async def slots_page(request: Request, state=Depends(get_state_func)):
        """List all slots."""
        slots = state.slot_manager.list_slots()
        return templates.TemplateResponse(request, "slots.html", {
            "slots": slots,
        })

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
        packages_by_id = {
            package.id: package
            for package in state.model_cache.list_packages()
        }
        candidates_by_slot = _policy_candidates_by_slot(
            state.policy_engine.list_policies()
        )
        model_options = []
        for model in models:
            package = _package_for_model(model, packages_by_id)
            trust_checks = _model_trust_checks(model, package)
            deploy_gate = _deployment_gate(trust_checks)
            _, blocked_slots = _slot_deployment_options(
                model,
                [slot],
                candidates_by_slot,
            )
            if blocked_slots:
                label = "Target excluded"
                allowed = False
            elif deploy_gate["allowed"]:
                label = "Ready"
                allowed = True
            else:
                label = deploy_gate["detail"]
                allowed = False

            model_options.append({
                "model": model,
                "allowed": allowed,
                "label": label,
            })

        # Get override status
        has_override = state.slot_manager.has_active_override(slot_name)

        # Get recent decisions for this slot
        decisions = state.slot_manager.get_decision_log(slot_name, limit=10)

        return templates.TemplateResponse(request, "slot_detail.html", {
            "slot": slot,
            "models": models,
            "model_options": model_options,
            "ready_model_options_count": sum(
                1 for option in model_options if option["allowed"]
            ),
            "has_override": has_override,
            "decisions": decisions,
        })

    @router.post("/slots/{slot_name}/override", response_class=HTMLResponse)
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

        packages_by_id = {
            package.id: package
            for package in state.model_cache.list_packages()
        }
        package = _package_for_model(model, packages_by_id)
        trust_checks = _model_trust_checks(model, package)
        deploy_gate = _deployment_gate(trust_checks)
        if not deploy_gate["allowed"]:
            html = (
                '<div class="rounded border border-amber-200 bg-amber-50 '
                'px-3 py-2 text-sm text-amber-800">'
                f"Cannot override {slot_name} with {model.name} v{model.version}. "
                f"{deploy_gate['detail']}. Use Models to package and verify first."
                "</div>"
            )
            return HTMLResponse(html)

        slot = state.slot_manager.get_slot(slot_name)
        if slot is None:
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Target not found: {slot_name}</div>'
            )

        _, blocked_slots = _slot_deployment_options(
            model,
            [slot],
            _policy_candidates_by_slot(state.policy_engine.list_policies()),
        )
        if blocked_slots:
            html = (
                '<div class="rounded border border-amber-200 bg-amber-50 '
                'px-3 py-2 text-sm text-amber-800">'
                f"Cannot override {slot_name} with {model.name} v{model.version}; "
                "the target candidate list excludes this model."
                "</div>"
            )
            return HTMLResponse(html)

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
            )

            return HTMLResponse(
                f'<div class="text-green-600 p-2">Override set: {model_name}</div>'
            )
        except Exception as e:
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Error: {e}</div>'
            )

    @router.post("/slots/{slot_name}/clear-override", response_class=HTMLResponse)
    async def clear_override(
        request: Request,
        slot_name: str,
        state=Depends(get_state_func),
    ):
        """Clear operator override on a slot."""
        state.slot_manager.clear_operator_override(slot_name)
        return HTMLResponse(
            '<div class="text-green-600 p-2">Override cleared</div>'
        )

    # ---- Conditions ----

    @router.get("/conditions", response_class=HTMLResponse)
    async def conditions_page(request: Request, state=Depends(get_state_func)):
        """List all conditions with injection form."""
        conditions = _condition_values(state.condition_store.get_all())
        operator_conditions_count = sum(
            1
            for condition in conditions
            if condition.get("source") in {"operator_api", "web_ui"}
        )
        return templates.TemplateResponse(request, "conditions.html", {
            "conditions": conditions,
            "operator_conditions_count": operator_conditions_count,
        })

    @router.post("/conditions/inject", response_class=HTMLResponse)
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

    @router.post("/conditions/clear-overrides", response_class=HTMLResponse)
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
        slot: str | None = None,
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
                decisions.extend(
                    state.slot_manager.get_decision_log(s.name, limit=50)
                )
            decisions.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            decisions = decisions[:50]

        slots = state.slot_manager.list_slots()

        return templates.TemplateResponse(request, "decisions.html", {
            "decisions": decisions,
            "slots": slots,
            "selected_slot": slot,
        })

    # ---- Models ----

    @router.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request, state=Depends(get_state_func)):
        """Hub model inventory with registry and edge-ready models."""
        return templates.TemplateResponse(
            request,
            "models.html",
            _hub_models_context(request, state),
        )

    @router.post("/models/import-mlflow", response_class=HTMLResponse)
    async def import_mlflow_model(
        request: Request,
        model_name: str = Form(...),
        model_version: str = Form(...),
        state=Depends(get_state_func),
    ):
        """Import one MLflow registry model version into the Hub cache."""
        existing = state.model_cache.find_model(model_name, model_version)
        if existing is not None:
            html = (
                '<div class="rounded border border-amber-200 bg-amber-50 '
                'px-3 py-2 text-sm text-amber-800">'
                f"{model_name} v{model_version} is already in Hub."
                "</div>"
            )
            return HTMLResponse(html, headers=_hub_refresh_headers())

        try:
            from temms.core.package import PackageImporter
            from temms.mlflow_bridge import MLflowBridge

            bridge = MLflowBridge()
            if not bridge.available:
                html = (
                    '<div class="rounded border border-red-200 bg-red-50 '
                    'px-3 py-2 text-sm text-red-700">'
                    "MLflow integration is not installed in this runtime."
                    "</div>"
                )
                return HTMLResponse(html)

            cache_dir = _hub_cache_dir(state)
            staged_dir = (
                cache_dir
                / "registry-imports"
                / _safe_slug(f"{model_name}-{model_version}")
            )
            package_dir = bridge.pull_model(
                model_name,
                version=model_version,
                dest_dir=staged_dir,
            )
            if package_dir is None:
                html = (
                    '<div class="rounded border border-red-200 bg-red-50 '
                    'px-3 py-2 text-sm text-red-700">'
                    f"Could not import {model_name} v{model_version} from MLflow."
                    "</div>"
                )
                return HTMLResponse(html)

            importer = PackageImporter(
                cache_dir=cache_dir,
                model_cache=state.model_cache,
                storage=state.model_storage,
            )
            result = importer.import_package(package_dir, verify=True)
            model_names = ", ".join(model.name for model in result.models)

            html = (
                '<div class="rounded border border-green-200 bg-green-50 '
                'px-3 py-2 text-sm text-green-700">'
                f"Imported {model_names or result.manifest.name}."
                "</div>"
            )
            return HTMLResponse(html, headers=_hub_refresh_headers())
        except Exception as e:
            logger.exception("MLflow import failed")
            html = (
                '<div class="rounded border border-red-200 bg-red-50 '
                'px-3 py-2 text-sm text-red-700">'
                f"Import failed: {e}"
                "</div>"
            )
            return HTMLResponse(html)

    @router.post("/models/deploy", response_class=HTMLResponse)
    async def deploy_model(
        request: Request,
        model_id: str = Form(...),
        slot_name: str = Form(...),
        reason: str = Form("Models deployment"),
        state=Depends(get_state_func),
    ):
        """Deploy a cached Hub model into an edge/edge-sim target."""
        model = state.model_cache.get_model(model_id)
        if model is None:
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Model not found: {model_id}</div>'
            )

        slot = state.slot_manager.get_slot(slot_name)
        if slot is None:
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Target not found: {slot_name}</div>'
            )

        packages_by_id = {
            package.id: package
            for package in state.model_cache.list_packages()
        }
        package = _package_for_model(model, packages_by_id)
        trust_checks = _model_trust_checks(model, package)
        deploy_gate = _deployment_gate(trust_checks)
        if not deploy_gate["allowed"]:
            html = (
                '<div class="rounded border border-amber-200 bg-amber-50 '
                'px-3 py-2 text-sm text-amber-800">'
                f"Cannot deploy {model.name} v{model.version}. "
                f"{deploy_gate['detail']}."
                "</div>"
            )
            return HTMLResponse(html)

        _, blocked_slots = _slot_deployment_options(
            model,
            [slot],
            _policy_candidates_by_slot(state.policy_engine.list_policies()),
        )
        if blocked_slots:
            html = (
                '<div class="rounded border border-amber-200 bg-amber-50 '
                'px-3 py-2 text-sm text-amber-800">'
                f"Cannot deploy {model.name} v{model.version} to {slot_name}; "
                "the target candidate list excludes this model."
                "</div>"
            )
            return HTMLResponse(html)

        try:
            await state.inference_runtime.load_model(slot_name, model.id)
            state.slot_manager.set_operator_override(
                slot_name=slot_name,
                model_id=model.id,
                reason=reason or "Models deployment",
                source="hub_ui",
            )
            state.slot_manager.activate_model(
                slot_name=slot_name,
                model_id=model.id,
                trigger_type="operator",
                trigger_detail=reason or "Models deployment",
                conditions=state.condition_store.get_snapshot(),
            )
            html = (
                '<div class="rounded border border-green-200 bg-green-50 '
                'px-3 py-2 text-sm text-green-700">'
                f"Deployed {model.name} v{model.version} to {slot_name}."
                "</div>"
            )
            return HTMLResponse(html, headers=_hub_refresh_headers())
        except Exception as e:
            logger.exception("Hub deployment failed")
            html = (
                '<div class="rounded border border-red-200 bg-red-50 '
                'px-3 py-2 text-sm text-red-700">'
                f"Deploy failed: {e}"
                "</div>"
            )
            return HTMLResponse(html)

    # ---- Import ----

    @router.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request, state=Depends(get_state_func)):
        """Compatibility route for the old import page."""
        return templates.TemplateResponse(
            request,
            "models.html",
            _hub_models_context(request, state),
        )

    @router.post("/import", response_class=HTMLResponse)
    async def do_import(
        request: Request,
        package_path: str = Form(...),
        state=Depends(get_state_func),
    ):
        """Import a package from the given path."""
        from temms.core.package import PackageImporter

        path = Path(package_path)
        if not path.exists():
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Path not found: {package_path}</div>'
            )

        try:
            importer = PackageImporter(
                cache_dir=_hub_cache_dir(state),
                model_cache=state.model_cache,
                storage=state.model_storage,
            )
            result = importer.import_package(path, verify=True)

            msg = (
                f"Imported {len(result.models)} models, "
                f"{len(result.policies)} policies from {result.manifest.name}"
            )
            return HTMLResponse(
                f'<div class="text-green-600 p-2">{msg}</div>',
                headers=_hub_refresh_headers(),
            )
        except Exception as e:
            return HTMLResponse(
                f'<div class="text-red-600 p-2">Import failed: {e}</div>'
            )

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
            badge_class = (
                "inline-flex items-center px-2 py-1 rounded-full text-xs "
                f"font-medium bg-{state_color}-100 text-{state_color}-800"
            )
            details_class = (
                "text-sm text-blue-500 hover:underline mt-2 block"
            )

            html_parts.append(f'''
            <div class="bg-white rounded-lg shadow p-4">
                <div class="flex justify-between items-center mb-2">
                    <h3 class="font-semibold text-lg">{slot.name}</h3>
                    <span class="{badge_class}">
                        {slot.state.value}
                    </span>
                </div>
                <p class="text-sm text-gray-600">{slot.description}</p>
                <p class="text-sm mt-2">
                    <span class="font-medium">Model:</span>
                    <span class="text-blue-600">{slot.active_model_id or "none"}</span>
                </p>
                <a href="/ui/slots/{slot.name}" class="{details_class}">Details &rarr;</a>
            </div>
            ''')

        return HTMLResponse("".join(html_parts))

    return router
