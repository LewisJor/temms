"""
TEMMS - Tactical Edge Model Management System

Adaptive inference control and decision evidence for disconnected edge devices.

TEMMS runs beside an edge inference stack, chooses among already-packaged
candidate models from local conditions and policy, and records why each model
activation happened.

Hub-and-daemon architecture:
- TEMMS Hub: model inventory, packaging, signing, and targeted container tests
- TEMMS Daemon: local runtime, policy evaluation, hot-swap, fallback, evidence
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
