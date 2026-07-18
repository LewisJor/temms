#!/usr/bin/env python3
"""Generate conformance vectors for temms-canonical-json/v1.

Writes tests/vectors/canonical_json_vectors.json: input -> expected SHA-256 over
the canonical serialization (see docs/proof-canonicalization.md). Run after an
intentional change to the canonicalization contract, then review the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

from temms.core.mission_package import canonical_json_hash

# Cases chosen to pin the rules that actually bite: integer-valued floats
# collapsing to ints, genuine floats being preserved, booleans not being
# coerced, key ordering, nesting, unicode, and edge shapes.
CASES: list[dict[str, object]] = [
    {"name": "empty_object", "input": {}},
    {"name": "empty_array", "input": []},
    {"name": "integer_valued_floats", "input": {"a": 12.0, "b": 4096.0, "c": -3.0}},
    {"name": "genuine_floats", "input": {"confidence": 0.72, "ratio": 0.5}},
    {"name": "booleans_are_not_numbers", "input": {"t": True, "f": False, "n": None}},
    {"name": "key_ordering", "input": {"z": 1.0, "a": 2.0, "m": {"y": 3.0, "b": 4.0}}},
    {"name": "nested_mixed", "input": {"a": [3.0, {"k": 4.0, "v": 0.25}], "z": "x"}},
    {"name": "unicode_strings", "input": {"label": "fog->lowlight", "sat": "Ὧ0"}},
    {
        "name": "mission_slo",
        "input": {
            "latency_budget_ms": 12.0,
            "min_throughput_ips": 85.0,
            "confidence_threshold": 0.72,
            "require_go": True,
        },
    },
    {
        "name": "edge_inventory_memory",
        "input": {"memory": {"available_mb": 4096.0, "total_mb": 8192.0}},
    },
]


def main() -> None:
    vectors = [
        {"name": case["name"], "input": case["input"],
         "sha256": canonical_json_hash(case["input"])}
        for case in CASES
    ]
    out = Path(__file__).resolve().parents[1] / "tests" / "vectors" / "canonical_json_vectors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"schema_version": "temms-canonical-json/v1", "vectors": vectors},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(vectors)} vectors to {out}")


if __name__ == "__main__":
    main()
