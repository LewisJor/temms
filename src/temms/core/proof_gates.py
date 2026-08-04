"""Edge-runtime proof gates — the single source of truth.

These gates decide whether an edge-runtime proof is acceptable: readiness status,
runtime-fit score, best-target selection, and the runtime capability lock. Both
the Hub (when generating and enforcing a proof) and the CLI (when verifying one
offline with ``temms verify-edge-proof``) must reach **identical** verdicts —
otherwise an operator in the field could verify a proof the Hub would block, and
the "signed, verifiable proof" guarantee is hollow.

This module exists because they previously did not. ``cli/main.py`` and
``hub_lite.py`` each carried a hand-maintained copy of every gate function, and
one copy had drifted: the Hub's capability-lock resolver omitted
``runtime_context`` itself as a source, so a proof carrying the lock at the top
level verified as PASS in the CLI and BLOCK in the Hub. Notably the Hub's own
*target-selection* resolver did search ``runtime_context``, so the two resolvers
disagreed inside a single file — the drift was accidental, not intentional.

Both call sites now delegate here. There is one implementation, so there is one
verdict.
"""

from __future__ import annotations

from typing import Any

# Where a proof may carry its runtime evidence. Ordered: the context itself
# first, then its nested contracts, then the same nested contracts under
# ``readiness``. Shared by every resolver so no two gates disagree about where
# to look.
_NESTED_SOURCE_KEYS = (
    "edge_execution_contract",
    "runtime_decision",
    "runtime_fit",
)

GATE_ACTIONS = frozenset({"readiness", "edge-runtime-mission"})


def optional_float(value: Any) -> float | None:
    """Coerce to float, or None when the value is missing/non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_sources(runtime_context: Any) -> list[dict[str, Any]]:
    """Every dict a proof may carry runtime evidence in, in resolution order."""
    if not isinstance(runtime_context, dict):
        return []
    sources: list[Any] = [runtime_context]
    sources.extend(runtime_context.get(key) for key in _NESTED_SOURCE_KEYS)
    readiness = runtime_context.get("readiness")
    if isinstance(readiness, dict):
        sources.extend(readiness.get(key) for key in _NESTED_SOURCE_KEYS)
    return [source for source in sources if isinstance(source, dict)]


def _first_present(runtime_context: Any, field: str) -> dict[str, Any]:
    """Return the first non-empty dict at ``field`` across the candidate sources."""
    for source in _candidate_sources(runtime_context):
        value = source.get(field)
        if isinstance(value, dict) and value:
            return value
    return {}


def runtime_target_selection(runtime_context: Any) -> dict[str, Any]:
    """Resolve the target-selection proof from a runtime context."""
    return _first_present(runtime_context, "target_selection")


def runtime_capability_lock(runtime_context: Any) -> dict[str, Any]:
    """Resolve the runtime capability lock from a runtime context."""
    return _first_present(runtime_context, "runtime_capability_lock")


def runtime_fit_score(action: str, payload: dict[str, Any]) -> float | None:
    """Extract the runtime fit score for a readiness or mission payload."""
    if action == "readiness":
        runtime_fit = payload.get("runtime_fit")
    else:
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        runtime_fit = metrics.get("runtime_fit")
    if not isinstance(runtime_fit, dict):
        return None
    return optional_float(runtime_fit.get("score"))


def runtime_target_best_failures(runtime_context: Any) -> list[str]:
    """Gate: the selected runtime target must be the best measured target."""
    target_selection = runtime_target_selection(runtime_context)
    if not target_selection:
        return ["runtime target selection proof is missing"]

    status = str(target_selection.get("status") or "").lower()
    selected = str(target_selection.get("selected_runtime_target_id") or "")
    best = str(target_selection.get("best_runtime_target_id") or "")
    score_delta = optional_float(target_selection.get("score_delta"))
    selected_is_best = bool(selected and best and selected == best)

    if (
        status == "best"
        and (not selected or not best or selected_is_best)
        and (score_delta is None or score_delta <= 0)
    ):
        return []
    if selected_is_best and (score_delta is None or score_delta <= 0):
        return []
    if selected and best and selected != best:
        return [f"selected runtime target {selected} is not best measured target {best}"]
    if score_delta is not None and score_delta > 0:
        return [
            f"selected runtime target trails best measured target by {score_delta:g} points"
        ]
    if status:
        return [f"runtime target selection status is {status}, expected best"]
    return ["runtime target selection proof is missing best-runtime status"]


def runtime_capability_lock_failures(runtime_context: Any) -> list[str]:
    """Gate: the runtime capability lock must be present, locked, and digest-valid."""
    lock = runtime_capability_lock(runtime_context)
    if not lock:
        return ["runtime capability lock proof is missing"]

    status = str(lock.get("status") or "").lower()
    digest = str(lock.get("capability_sha256") or "")
    raw_failures = lock.get("failures") if isinstance(lock.get("failures"), list) else []
    lock_failures = [str(failure) for failure in raw_failures if failure]

    failures: list[str] = []
    if status != "locked":
        failures.append(
            f"runtime capability lock status is {status or 'missing'}, expected locked"
        )
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        failures.append("runtime capability lock capability_sha256 is missing or invalid")
    if lock_failures:
        failures.append(
            "runtime capability lock has failures: " + "; ".join(lock_failures[:3])
        )
    return failures


def proof_gate_failures(
    action: str,
    payload: dict[str, Any],
    *,
    require_go: bool,
    min_runtime_fit: float | None,
    require_best_runtime: bool = False,
    require_capability_lock: bool = False,
    runtime_context: dict[str, Any] | None = None,
) -> list[str]:
    """Return every gate failure for an edge-runtime proof.

    The one implementation behind both Hub enforcement and CLI verification.
    """
    if action not in GATE_ACTIONS:
        return []

    failures: list[str] = []
    status = str(payload.get("status") or "unknown")
    if require_go and status != "go":
        failures.append(f"{action} status is {status}, expected go")

    if min_runtime_fit is not None:
        score = runtime_fit_score(action, payload)
        if score is None:
            failures.append("runtime fit score is missing")
        elif score < min_runtime_fit:
            failures.append(
                f"runtime fit score {score:g}/100 is below required {min_runtime_fit:g}/100"
            )

    context = runtime_context or payload or {}
    if require_best_runtime:
        failures.extend(runtime_target_best_failures(context))
    if require_capability_lock:
        failures.extend(runtime_capability_lock_failures(context))
    return failures
