"""Minimal local ONNX Runtime adapter."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from temms.errors import ModelUnavailableError, NoActiveModelError, RuntimeDependencyError
from temms.types import ActivationResult, InferenceResult, ModelRef, RuntimeState, Scalar

OnnxInput = Mapping[str, object]
OnnxOutput = tuple[object, ...]


class _Session(Protocol):
    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: Mapping[str, object],
    ) -> Sequence[object]: ...


SessionFactory = Callable[[str, Sequence[str] | None], _Session]


@dataclass(frozen=True, slots=True)
class OnnxModel:
    """One locally accessible ONNX artifact."""

    ref: ModelRef
    path: Path
    providers: tuple[str, ...] = ()
    warmup: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _LoadedModel:
    config: OnnxModel
    session: _Session


class OnnxRuntime:
    """Load local ONNX files, atomically swap sessions, and run inference."""

    def __init__(
        self,
        *,
        models: Mapping[str, Sequence[OnnxModel]],
        resources: Mapping[str, Scalar] | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._models = {slot: tuple(items) for slot, items in models.items()}
        self._resources = dict(resources or {})
        self._session_factory = session_factory or _create_session
        self._active: dict[str, _LoadedModel] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._validate_inventory()

    async def models(self, slot: str) -> Sequence[ModelRef]:
        return tuple(model.ref for model in self._models.get(slot, ()))

    async def state(self, slot: str) -> RuntimeState:
        with self._lock_for(slot):
            active = self._active.get(slot)
        return RuntimeState(
            active_model=active.config.ref if active else None,
            resources=dict(self._resources),
        )

    async def activate(self, slot: str, model: ModelRef) -> ActivationResult:
        config = self._find(slot, model)
        loaded = await asyncio.to_thread(self._load, config)
        with self._lock_for(slot):
            previous = self._active.get(slot)
            self._active[slot] = loaded
        return ActivationResult(
            requested_model=model,
            active_model=config.ref,
            changed=previous is None
            or _identity(previous.config.ref) != _identity(config.ref),
        )

    async def infer(self, slot: str, value: OnnxInput) -> InferenceResult[OnnxOutput]:
        with self._lock_for(slot):
            loaded = self._active.get(slot)
        if loaded is None:
            raise NoActiveModelError(f"slot {slot!r} has no active model")
        output = await asyncio.to_thread(loaded.session.run, None, dict(value))
        return InferenceResult(model=loaded.config.ref, output=tuple(output))

    def update_resources(self, **resources: Scalar) -> None:
        """Change the resource state exposed to the selector."""
        self._resources.update(resources)

    def _load(self, config: OnnxModel) -> _LoadedModel:
        if not config.path.is_file():
            raise ModelUnavailableError(f"ONNX model file does not exist: {config.path}")
        providers = config.providers or None
        session = self._session_factory(str(config.path), providers)
        if config.warmup is not None:
            session.run(None, dict(config.warmup))
        return _LoadedModel(config=config, session=session)

    def _find(self, slot: str, model: ModelRef) -> OnnxModel:
        identity = model.id, model.digest
        for candidate in self._models.get(slot, ()):
            if (candidate.ref.id, candidate.ref.digest) == identity:
                return candidate
        raise ModelUnavailableError(
            f"model {model.id!r} ({model.digest}) is unavailable for slot {slot!r}"
        )

    def _lock_for(self, slot: str) -> threading.RLock:
        lock = self._locks.get(slot)
        if lock is None:
            lock = self._locks[slot] = threading.RLock()
        return lock

    def _validate_inventory(self) -> None:
        for slot, models in self._models.items():
            identities = [(model.ref.id, model.ref.digest) for model in models]
            if len(identities) != len(set(identities)):
                raise ValueError(f"slot {slot!r} contains duplicate model identities")


def _identity(model: ModelRef) -> tuple[str, str]:
    return model.id, model.digest


def _create_session(path: str, providers: Sequence[str] | None) -> _Session:
    try:
        import onnxruntime
    except ImportError as exc:
        raise RuntimeDependencyError(
            "OnnxRuntime requires the optional 'onnx' extra: pip install 'temms[onnx]'"
        ) from exc
    options = {} if providers is None else {"providers": list(providers)}
    return cast(_Session, onnxruntime.InferenceSession(path, **options))
