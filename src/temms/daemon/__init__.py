"""TEMMS daemon package exports."""

from typing import Any

__all__ = ["TEMMSDaemon", "DaemonConfig"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from temms.daemon.service import DaemonConfig, TEMMSDaemon

        return {"DaemonConfig": DaemonConfig, "TEMMSDaemon": TEMMSDaemon}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
