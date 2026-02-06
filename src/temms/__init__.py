"""
TEMMS - Tactical Edge Model Management System

An offline-first ML model management system for edge devices operating in DDIL
(Denied, Degraded, Intermittent, Limited connectivity) environments.

Three-tier architecture:
- MLflow (Cloud): Standard registry, not modified
- TEMMS Hub: DDIL sync layer, packages models
- TEMMS Daemon: Edge runtime, policy-driven switching
"""

__version__ = "0.1.0"
__author__ = "TEMMS Team"

from temms.core.config import Config
from temms.core.cache import ModelCache
from temms.core.package import PackageManifest, PackageImporter
from temms.slots.manager import SlotManager
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine

__all__ = [
    "Config",
    "ModelCache",
    "PackageManifest",
    "PackageImporter",
    "SlotManager",
    "ConditionStore",
    "PolicyEngine",
    "__version__",
]
