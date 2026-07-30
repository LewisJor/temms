"""The ``mission.yaml`` schema — mission control for a model portfolio (issue #43).

One authored YAML **is** the package definition. It declares the model portfolio
(each member an operating point with an ``optimal_when`` preference region and a
``requires`` feasibility block) and the targeting; ``manifest.json`` is compiled
from it and signed. This module owns parsing and validation; compilation lives in
``mission_compiler``.

The keystone validation lives here: **the spec is rejected if its policy
references a model the portfolio does not carry.** That single check converts
policy-driven degradation from "works because the demo pre-seeded the cache" into
"provably works offline" — a build error on the laptop instead of a silent
"model not found" on a disconnected edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from temms.policy.schema import SlotPolicy

MISSION_PACKAGE_SPEC_SCHEMA_VERSION = "temms-mission-package-spec/v1"


class MissionSpecError(ValueError):
    """A mission.yaml is malformed or internally inconsistent."""


class ModelEnvelope(BaseModel):
    """One portfolio member: an operating point.

    ``optimal_when`` is the preference region (which conditions this model is
    *for*); ``requires`` is the feasibility block (what it needs to run at all).
    Slice 1 parses and carries these; best-feasible dispatch consumes them in a
    later slice.
    """

    id: str
    source: str
    format: str
    provides: str | None = None
    optimal_when: dict[str, Any] = Field(default_factory=dict)
    requires: dict[str, Any] = Field(default_factory=dict)


class MissionSlot(BaseModel):
    """The slot this portfolio serves, and the policy that governs switching."""

    name: str
    policy: str | None = None  # path to a SlotPolicy YAML, relative to mission.yaml


class MissionTarget(BaseModel):
    """What the portfolio is allowed to run on. Drives runtime target + fit."""

    device_profiles: list[str] = Field(default_factory=list)
    runtime: str | None = None
    requires: dict[str, Any] = Field(default_factory=dict)


class MissionMetadata(BaseModel):
    name: str
    version: str = "0.1.0"
    description: str = ""


class MissionPackageSpec(BaseModel):
    """The parsed mission.yaml."""

    apiVersion: str = "temms/v1"
    kind: str = "MissionPackage"
    metadata: MissionMetadata
    models: list[ModelEnvelope]
    slot: MissionSlot
    target: MissionTarget = Field(default_factory=MissionTarget)

    def model_ids(self) -> list[str]:
        return [m.id for m in self.models]


def load_mission_spec(path: Path) -> MissionPackageSpec:
    """Parse and fully validate a mission.yaml, including policy references.

    Raises MissionSpecError with an actionable message on any problem.
    """
    path = Path(path)
    if not path.is_file():
        raise MissionSpecError(f"mission spec not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MissionSpecError(f"mission spec is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise MissionSpecError("mission spec must be a YAML mapping")

    try:
        spec = MissionPackageSpec(**raw)
    except ValidationError as exc:
        raise MissionSpecError(f"mission spec is invalid:\n{exc}") from exc

    _validate_structure(spec)
    _validate_policy_references(spec, mission_dir=path.parent)
    return spec


def _validate_structure(spec: MissionPackageSpec) -> None:
    if spec.kind != "MissionPackage":
        raise MissionSpecError(f"kind must be MissionPackage, got {spec.kind!r}")
    if not spec.models:
        raise MissionSpecError("a mission must declare at least one model")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for model in spec.models:
        if model.id in seen:
            duplicates.add(model.id)
        seen.add(model.id)
    if duplicates:
        raise MissionSpecError(f"duplicate model id(s): {sorted(duplicates)}")


def policy_referenced_models(policy: SlotPolicy) -> set[str]:
    """Every model name a policy can select: default, rule targets, preloads."""
    referenced: set[str] = set()
    if policy.spec.default_model:
        referenced.add(policy.spec.default_model)
    referenced.update(policy.spec.fallback_chain)
    for rule in policy.spec.rules:
        referenced.add(rule.action.switch_to)
        if rule.action.preload:
            referenced.update(rule.action.preload)
    return referenced


def _validate_policy_references(spec: MissionPackageSpec, *, mission_dir: Path) -> None:
    """The keystone check: the policy may only reference carried models."""
    if not spec.slot.policy:
        return

    policy_path = Path(spec.slot.policy)
    if not policy_path.is_absolute():
        policy_path = mission_dir / policy_path
    if not policy_path.is_file():
        raise MissionSpecError(f"slot policy not found: {policy_path}")

    try:
        policy = SlotPolicy.from_yaml(policy_path)
    except (ValidationError, yaml.YAMLError) as exc:
        raise MissionSpecError(f"slot policy {policy_path} is invalid: {exc}") from exc

    if policy.spec.slot != spec.slot.name:
        raise MissionSpecError(
            f"policy is for slot {policy.spec.slot!r} but the mission slot is "
            f"{spec.slot.name!r}"
        )

    carried = set(spec.model_ids())
    missing = sorted(policy_referenced_models(policy) - carried)
    if missing:
        raise MissionSpecError(
            "policy references model(s) the portfolio does not carry: "
            f"{missing}. Every model a policy can switch to (including its "
            "fallback chain) must be declared under models[], or the edge would "
            "fail to switch to it offline. Carried models: "
            f"{sorted(carried)}."
        )
