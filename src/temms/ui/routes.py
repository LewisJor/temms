"""TEMMS Web UI routes.

Serves the compiled React Hub (Mission Package Workbench) and redirects the
retired diagnostic paths to it. The daemon always configures Hub Lite, so the
Hub is the only UI; the former server-rendered Jinja diagnostic pages
(dashboard, slots, conditions, decisions, models, import) have been removed —
the React app talks to the ``/v1/hub`` and ``/v1/control`` APIs directly.
"""

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
HUB_STATIC_DIR = STATIC_DIR / "hub"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Retired diagnostic GET paths that now redirect to the React Hub. Kept as
# redirects (not 404s) so bookmarks and older links land on the product cockpit.
_RETIRED_UI_PATHS = (
    "/",
    "/dashboard",
    "/slots",
    "/slots/{slot_name}",
    "/conditions",
    "/decisions",
    "/models",
    "/import",
    "/operate",
    "/runtimes",
)


def _template_response(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    context.setdefault("request", request)
    return templates.TemplateResponse(request, name, context)


def _hub_app_assets() -> dict[str, list[str]]:
    """Return Vite-built Hub app assets, if the React bundle has been built."""
    manifest_path = HUB_STATIC_DIR / ".vite" / "manifest.json"
    if not manifest_path.exists():
        return {"scripts": [], "styles": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read Hub UI manifest")
        return {"scripts": [], "styles": []}

    entry = manifest.get("index.html") if isinstance(manifest, dict) else None
    if not isinstance(entry, dict):
        return {"scripts": [], "styles": []}
    scripts = [entry["file"]] if isinstance(entry.get("file"), str) else []
    styles = entry.get("css") if isinstance(entry.get("css"), list) else []
    return {
        "scripts": [f"/ui/assets/hub/{path}" for path in scripts],
        "styles": [f"/ui/assets/hub/{path}" for path in styles if isinstance(path, str)],
    }


def create_ui_router(get_state_func, control_auth_dependency=None) -> APIRouter:
    """Create the UI router.

    ``control_auth_dependency`` is accepted for call-site compatibility; there
    are no UI write endpoints anymore (the React Hub uses the ``/v1`` APIs).
    """
    router = APIRouter(prefix="/ui", tags=["ui"])

    @router.get("/assets/hub/{asset_path:path}")
    async def hub_app_asset(asset_path: str):
        """Serve compiled Vite assets for the React Hub app."""
        candidate = (HUB_STATIC_DIR / asset_path).resolve()
        static_root = HUB_STATIC_DIR.resolve()
        if not candidate.is_file() or static_root not in candidate.parents:
            raise HTTPException(status_code=404, detail="Hub UI asset not found")
        headers = {}
        if asset_path.startswith("assets/"):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return FileResponse(candidate, headers=headers)

    @router.get("/hub", response_class=HTMLResponse)
    async def hub_page(request: Request, state=Depends(get_state_func)):
        """Serve the React Hub (Mission Package Workbench) shell."""
        if getattr(state, "hub_lite", None) is None:
            raise HTTPException(status_code=404, detail="Hub Lite is not configured")
        return _template_response(
            request,
            "hub.html",
            {"request": request, "hub_assets": _hub_app_assets()},
        )

    def _make_retired_redirect():
        async def _redirect(state=Depends(get_state_func)):
            if getattr(state, "hub_lite", None) is None:
                raise HTTPException(status_code=404, detail="Hub Lite is not configured")
            return RedirectResponse(url="/ui/hub", status_code=307)

        return _redirect

    for path in _RETIRED_UI_PATHS:
        router.add_api_route(
            path,
            _make_retired_redirect(),
            methods=["GET"],
            include_in_schema=False,
        )

    return router
