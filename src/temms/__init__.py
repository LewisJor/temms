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

from temms.conditions.store import ConditionStore
from temms.core.cache import ModelCache
from temms.core.config import Config
from temms.core.package import PackageImporter, PackageManifest
from temms.policy.engine import PolicyEngine
from temms.slots.manager import SlotManager

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
