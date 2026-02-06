"""
TEMMS Inference Server module.

Provides FastAPI-based inference endpoints for model serving.
"""

from temms.inference.server import app, create_app

__all__ = ["app", "create_app"]
