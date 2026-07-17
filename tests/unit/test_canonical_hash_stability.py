"""Digest stability across JSON/JS number round-trips (UI stage-rollout bug).

The React UI plans a mission package, then stages the plan it received. Between
those calls the plan is serialized to JSON, parsed by JavaScript (which has only
IEEE-754 doubles), and sent back, collapsing integer-valued floats like ``12.0``
to ``12``. The mission-package payload digest must survive that round-trip or
staging fails with "payload digest does not match artifact body".
"""

from __future__ import annotations

from temms.core.mission_package import canonical_json_hash as core_hash


def _js_number_roundtrip(value):
    """Mimic a JSON -> JS -> JSON round-trip: integer-valued floats become ints."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _js_number_roundtrip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_js_number_roundtrip(v) for v in value]
    return value


def test_hash_is_stable_across_js_number_roundtrip():
    payload = {
        "slo": {"latency_budget_ms": 12.0, "min_throughput_ips": 85.0},
        "confidence_threshold": 0.72,
        "edge_inventory": {"memory": {"available_mb": 4096.0, "total_mb": 8192.0}},
        "score_delta": 0.0,
        "nested": [{"x": 1.0}, {"x": 2.5}],
    }
    assert canonical_json_hash_equal(payload, _js_number_roundtrip(payload))


def canonical_json_hash_equal(a, b) -> bool:
    return core_hash(a) == core_hash(b)


def test_genuine_floats_are_preserved():
    # A non-integer float must still affect the hash (we only normalize .0 floats).
    assert core_hash({"x": 0.72}) != core_hash({"x": 0.73})
    assert core_hash({"x": 1.5}) != core_hash({"x": 2.5})


def test_all_modules_share_one_hash_implementation():
    # hub_lite and the CLI must not drift from the core source of truth.
    from temms.cli.main import _canonical_json_hash
    from temms.hub_lite import canonical_json_hash as hub_hash

    payload = {"a": 1.0, "b": [2.0, 3.5], "c": {"d": 4096.0}}
    assert hub_hash(payload) == core_hash(payload)
    assert _canonical_json_hash(payload) == core_hash(payload)
    # And all agree after a JS round-trip.
    rt = _js_number_roundtrip(payload)
    assert hub_hash(rt) == core_hash(payload) == _canonical_json_hash(rt)
