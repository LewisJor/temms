"""
Policy schema definitions using Pydantic.
"""

from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field
from pathlib import Path
import yaml


class Condition(BaseModel):
    """A single condition to evaluate."""
    metric: str  # Condition path, e.g., "weather.visibility_m"
    operator: str  # eq, neq, gt, gte, lt, lte, in, not_in
    value: Any
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ConditionGroup(BaseModel):
    """Group of conditions with AND/OR logic."""
    all: Optional[List[Condition]] = None  # AND
    any: Optional[List[Condition]] = None  # OR


class PolicyAction(BaseModel):
    """Action to take when rule matches."""
    switch_to: str  # Model name to switch to
    version: Optional[str] = None  # Pin specific version (default: latest imported)
    preload: Optional[List[str]] = None  # Models to preload (not activate)


class PolicyRule(BaseModel):
    """A policy rule with conditions and action."""
    name: str
    priority: int = Field(default=50)  # Higher = evaluated first
    conditions: ConditionGroup
    action: PolicyAction
    # Anti-flap hysteresis: the rule's conditions must hold continuously for at
    # least this many seconds before the rule may fire a switch. 0 (default)
    # preserves immediate switching. Dampens a sensor flapping across a
    # threshold from thrashing model swaps (swap-contract: hysteresis).
    min_dwell_s: float = Field(default=0.0, ge=0.0)


class SlotPolicyMetadata(BaseModel):
    """Policy metadata."""
    name: str
    description: Optional[str] = None


class SlotPolicySpec(BaseModel):
    """Policy specification."""
    slot: str  # Which slot this policy controls
    default_model: Optional[str] = None  # Model to use when no rules match
    rules: List[PolicyRule]
    allow_operator_override: bool = Field(default=True)
    fallback_chain: List[str] = Field(default_factory=list)


class SlotPolicy(BaseModel):
    """Slot-aware policy definition."""
    apiVersion: str = Field(default="temms/v1")
    kind: str = Field(default="SlotPolicy")
    metadata: SlotPolicyMetadata
    spec: SlotPolicySpec

    @classmethod
    def from_yaml(cls, path: Path) -> "SlotPolicy":
        """Load policy from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def to_yaml(self, path: Path) -> None:
        """Save policy to YAML file."""
        with open(path, "w") as f:
            yaml.dump(
                self.model_dump(exclude_none=True),
                f,
                default_flow_style=False,
                sort_keys=False,
            )
