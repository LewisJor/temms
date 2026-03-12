"""
TEMMS Web UI routes using FastAPI + Jinja2 + HTMX.

Provides a lightweight dashboard for monitoring and controlling the TEMMS daemon.
No Node.js or frontend build step required — HTMX and Tailwind CSS loaded via CDN.
"""

import time
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
from urllib.parse import quote

from markupsafe import escape

from fastapi import APIRouter, Request, Depends, Form, HTTPException
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

    # ---- Dashboard ----

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, state=Depends(get_state_func)):
        """Main dashboard with system overview."""
        slots = state.slot_manager.list_slots()
        conditions = state.condition_store.get_all()
        policies = state.policy_engine.list_policies()
        uptime = time.time() - state.start_time

        # Determine system health
        has_error = any(s.state.value == "error" for s in slots)
        has_degraded = any(
            s.state.value != "running" and s.required for s in slots
        )

        if has_error:
            health = "error"
        elif has_degraded:
            health = "degraded"
        else:
            health = "healthy"

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

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "health": health,
            "slots": slots,
            "conditions": conditions[:10],
            "conditions_count": len(conditions),
            "policies": policies,
            "decisions": decisions,
            "uptime": uptime,
        })

    # ---- Slots ----

    @router.get("/slots", response_class=HTMLResponse)
    async def slots_page(request: Request, state=Depends(get_state_func)):
        """List all slots."""
        slots = state.slot_manager.list_slots()
        return templates.TemplateResponse("slots.html", {
            "request": request,
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

        # Get override status
        has_override = state.slot_manager.has_active_override(slot_name)

        # Get recent decisions for this slot
        decisions = state.slot_manager.get_decision_log(slot_name, limit=10)

        return templates.TemplateResponse("slot_detail.html", {
            "request": request,
            "slot": slot,
            "models": models,
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
        conditions = state.condition_store.get_all()
        return templates.TemplateResponse("conditions.html", {
            "request": request,
            "conditions": conditions,
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
                decisions.extend(
                    state.slot_manager.get_decision_log(s.name, limit=50)
                )
            decisions.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            decisions = decisions[:50]

        slots = state.slot_manager.list_slots()

        return templates.TemplateResponse("decisions.html", {
            "request": request,
            "decisions": decisions,
            "slots": slots,
            "selected_slot": slot,
        })

    # ---- Models ----

    @router.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request, state=Depends(get_state_func)):
        """List cached models."""
        models = state.model_cache.list_models()
        return templates.TemplateResponse("models.html", {
            "request": request,
            "models": models,
        })

    # ---- Import ----

    @router.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request, state=Depends(get_state_func)):
        """Package import page."""
        packages = state.model_cache.list_packages()
        return templates.TemplateResponse("import_page.html", {
            "request": request,
            "packages": packages,
        })

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
                cache_dir=path.parent,
                model_cache=state.model_cache,
                storage=state.model_storage,
            )
            result = importer.import_package(path, verify=True)

            msg = (
                f"Imported {len(result.models)} models, "
                f"{len(result.policies)} policies from {result.manifest.name}"
            )
            return HTMLResponse(
                f'<div class="text-green-600 p-2">{msg}</div>'
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

            slot_name = escape(slot.name)
            slot_name_url = quote(slot.name, safe="")
            slot_state = escape(slot.state.value)
            slot_description = escape(slot.description)
            slot_model = escape(slot.active_model_id or "none")

            html_parts.append(f'''
            <div class="bg-white rounded-lg shadow p-4">
                <div class="flex justify-between items-center mb-2">
                    <h3 class="font-semibold text-lg">{slot_name}</h3>
                    <span class="inline-flex items-center px-2 py-1 rounded-full text-xs font-medium bg-{state_color}-100 text-{state_color}-800">
                        {slot_state}
                    </span>
                </div>
                <p class="text-sm text-gray-600">{slot_description}</p>
                <p class="text-sm mt-2">
                    <span class="font-medium">Model:</span>
                    <span class="text-blue-600">{slot_model}</span>
                </p>
                <a href="/ui/slots/{slot_name_url}" class="text-sm text-blue-500 hover:underline mt-2 block">Details &rarr;</a>
            </div>
            ''')

        return HTMLResponse("".join(html_parts))

    return router
