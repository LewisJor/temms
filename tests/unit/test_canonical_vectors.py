"""Conformance vectors for temms-canonical-json/v1 (issue #28).

Two guards:
1. The reference implementation reproduces every committed vector, so an
   accidental change to the canonicalization fails CI (regenerate deliberately
   with scripts/gen_canonical_vectors.py).
2. An *independent* implementation written straight from the spec
   (docs/proof-canonicalization.md) reproduces the same digests — proving the
   contract is implementable by a JS/native client from the doc alone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from temms.core.mission_package import canonical_json_hash

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors" / "canonical_json_vectors.json").read_text()
)["vectors"]


def _spec_digest(value):
    """SHA-256 over the canonical serialization, implemented from the spec only."""

    def canon(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, dict):
            return {k: canon(item) for k, item in v.items()}
        if isinstance(v, list):
            return [canon(item) for item in v]
        return v

    text = json.dumps(canon(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_reference_matches_vectors(vector):
    assert canonical_json_hash(vector["input"]) == vector["sha256"]


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_independent_spec_implementation_matches_vectors(vector):
    assert _spec_digest(vector["input"]) == vector["sha256"]


def test_integer_valued_floats_and_ints_agree():
    assert canonical_json_hash({"x": 12.0}) == canonical_json_hash({"x": 12})


def test_non_ascii_is_utf8_not_escaped():
    # The canonical bytes contain raw UTF-8, so they match a JS client.
    expected = hashlib.sha256(
        json.dumps({"s": "Détecter🛰"}, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert canonical_json_hash({"s": "Détecter🛰"}) == expected
