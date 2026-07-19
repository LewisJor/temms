#!/usr/bin/env python3
"""Crash-atomicity soak: SIGKILL a real subprocess mid-write, then verify state (#29).

The in-process soak (scripts/soak.py) proves recovery from a *graceful* restart.
The DDIL credibility test is different: power is cut, or the supervisor kills the
process, in the middle of writing state. Nothing gets to run — no finally block,
no signal handler, no flush. Whatever is on disk is what the device wakes up to.

This harness spawns a real subprocess that hammers the production write paths
(deployment state, the signed intent queue, the decision chain, the trust store),
kills it with SIGKILL at a random offset, and then verifies on restart that:

  * every state file parses — no truncated or half-written JSON
  * the decision chain is intact and hash-linked
  * the intent queue is a consistent list of well-formed entries
  * recovery is deterministic: the active model is the last *committed* decision

Deliberate design note: the issue proposed driving a full `temms daemon`. Killing
a real daemon is slower to start than it is to kill, so a run buys only a handful
of kills — and the property under test is atomicity of the state writes, not
daemon startup. Driving the same store classes directly buys hundreds of kills in
the same wall-clock budget, which is what actually catches a torn write. The
write paths exercised are the production ones, unmodified.

Usage:
    python scripts/crash_soak.py --iterations 40
    python scripts/crash_soak.py --iterations 100 --report docs/crash-atomicity.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

MODELS = ["model-daylight", "model-lowlight", "model-tiny"]
SCHEMA_VERSION = "temms-crash-atomicity/v1"


# --------------------------------------------------------------------------
# Worker: runs in the child process and is killed mid-write.
# --------------------------------------------------------------------------


def run_worker(root: Path, seed: int) -> None:
    """Write DDIL state continuously until SIGKILLed. Never exits on its own."""
    from temms.core.trust_store import TrustStore
    from temms.daemon.deployment_state import DeploymentState, DeploymentStateStore
    from temms.daemon.pending_ops import PendingOperationsStore
    from temms.slots.manager import SlotManager

    if os.environ.get("TEMMS_CRASH_SOAK_UNSAFE_WRITES") == "1":
        _install_torn_writes()

    rng = random.Random(seed)
    slot_manager = SlotManager(root / "temms.db")
    if slot_manager.get_slot("vision") is None:
        slot_manager.create_slot("vision", "Vision", default_model=MODELS[0])

    state_store = DeploymentStateStore(path=root / "deployment_state.json")
    queue = PendingOperationsStore(path=root / "pending_ops.json")
    trust_path = root / "trust-store.json"

    states = [
        DeploymentState.PENDING,
        DeploymentState.DOWNLOADING,
        DeploymentState.READY,
    ]

    counter = 0
    while True:
        counter += 1
        model = MODELS[counter % len(MODELS)]

        # A swap: decision chain append, then the state file. A kill between
        # these two is exactly the interleaving we care about.
        slot_manager.activate_model(
            slot_name="vision",
            model_id=model,
            trigger_type="soak",
            trigger_detail=f"iteration-{counter}",
            conditions={"iteration": counter},
            audit_metadata={"model_id": model},
        )
        state_store.set_state(states[counter % len(states)], f"soak-{counter}")

        # Intent queue churn, including growth and drain.
        queue.enqueue("update_conditions", {"iteration": counter, "model": model})
        if counter % 5 == 0:
            queue.clear()

        # Trust store rewrite, to cover the path added in #31.
        if counter % 7 == 0:
            store = TrustStore.load(trust_path)
            store.keys.clear()
            store.add(_throwaway_public_key(rng), label=f"rotation-{counter}")
            store.save(trust_path)

        # No sleep: maximise writes per second so kills land inside one.


def _install_torn_writes() -> None:
    """Replace atomic writes with deliberately tearable ones (self-test only).

    A detector that never fires proves nothing. Setting
    TEMMS_CRASH_SOAK_UNSAFE_WRITES=1 makes the worker write JSON in two flushed
    chunks with a pause between them, so a SIGKILL can land mid-file. The soak
    is expected to FAIL under this flag — that failure is what demonstrates the
    harness would catch a real atomicity regression.
    """
    import temms.daemon.deployment_state as deployment_state
    import temms.daemon.pending_ops as pending_ops

    def torn_write_json(path: Path, data: Any, **kwargs: Any) -> None:
        text = json.dumps(data, indent=kwargs.get("indent", 2))
        midpoint = max(1, len(text) // 2)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text[:midpoint])
            handle.flush()
            os.fsync(handle.fileno())
            time.sleep(0.002)  # a window for SIGKILL to land inside the file
            handle.write(text[midpoint:])

    deployment_state.write_json_atomic = torn_write_json
    pending_ops.write_json_atomic = torn_write_json


def _throwaway_public_key(rng: random.Random) -> str:
    """A deterministic-ish Ed25519 public key for trust-store churn."""
    from temms.core.signing import generate_ed25519_keypair

    del rng  # keypair generation is not seedable; determinism is not needed here
    return generate_ed25519_keypair()[1]


# --------------------------------------------------------------------------
# Verification: runs in the parent after each kill.
# --------------------------------------------------------------------------


def _read_json(path: Path) -> tuple[bool, Any, str]:
    """Read JSON straight from disk.

    Deliberately not via the store accessors: DeploymentStateStore._read()
    swallows a corrupt file and reports PENDING, which would mask exactly the
    corruption this harness exists to detect.
    """
    if not path.exists():
        return True, None, "absent (never written)"
    try:
        return True, json.loads(path.read_text(encoding="utf-8")), ""
    except json.JSONDecodeError as exc:
        return False, None, f"corrupt JSON: {exc}"
    except OSError as exc:
        return False, None, f"unreadable: {exc}"


def verify_state(root: Path) -> list[dict[str, Any]]:
    """Check every artifact the killed worker was writing."""
    from temms.daemon.deployment_state import DeploymentState
    from temms.slots.manager import SlotManager

    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    # 1. Deployment state file is parseable and holds a legal state.
    ok, payload, detail = _read_json(root / "deployment_state.json")
    if not ok:
        record("deployment_state_parses", False, detail)
    elif payload is None:
        record("deployment_state_parses", True, detail)
    elif not isinstance(payload, dict):
        # A torn write can leave JSON that parses but is not an object. Record
        # it as a failed check rather than crashing the harness on .get().
        record(
            "deployment_state_parses",
            False,
            f"expected a JSON object, got {type(payload).__name__}",
        )
    else:
        valid_states = {s.value for s in DeploymentState}
        state = payload.get("state")
        record(
            "deployment_state_parses",
            state in valid_states,
            f"state={state!r}" if state in valid_states else f"illegal state {state!r}",
        )

    # 2. Intent queue is a list of well-formed entries.
    ok, entries, detail = _read_json(root / "pending_ops.json")
    if not ok:
        record("intent_queue_parses", False, detail)
    elif entries is None:
        record("intent_queue_parses", True, detail)
    elif not isinstance(entries, list):
        record("intent_queue_parses", False, f"expected a list, got {type(entries).__name__}")
    else:
        malformed = [
            e for e in entries if not isinstance(e, dict) or "operation" not in e
        ]
        record(
            "intent_queue_parses",
            not malformed,
            f"{len(entries)} entries, {len(malformed)} malformed",
        )

    # 3. Trust store is parseable.
    ok, _, detail = _read_json(root / "trust-store.json")
    record("trust_store_parses", ok, detail or "ok")

    # 4. Decision chain is intact, and recovery is deterministic.
    db_path = root / "temms.db"
    if not db_path.exists():
        record("decision_chain_intact", True, "no database yet")
        record("deterministic_recovery", True, "no database yet")
        return checks

    try:
        slot_manager = SlotManager(db_path)
    except Exception as exc:
        record("decision_chain_intact", False, f"database unusable after kill: {exc}")
        record("deterministic_recovery", False, "database unusable")
        return checks

    try:
        chain = slot_manager.verify_decision_chain()
        record(
            "decision_chain_intact",
            bool(chain.get("valid")),
            f"{slot_manager.decision_count()} entries"
            if chain.get("valid")
            else f"broken at {chain.get('broken_at')}: {chain.get('reason')}",
        )

        # The swap contract's anchor: the slot must come back on the last model
        # whose decision was actually committed, never a partially applied one.
        slot = slot_manager.get_slot("vision")
        decisions = slot_manager.fetchall(
            "SELECT to_model FROM slot_decisions ORDER BY id DESC LIMIT 1"
        )
        if not decisions:
            record("deterministic_recovery", True, "no decisions committed yet")
        else:
            last_committed = decisions[0]["to_model"]
            active = slot.active_model_id if slot else None
            record(
                "deterministic_recovery",
                active == last_committed,
                f"active={active} last_committed={last_committed}",
            )
    finally:
        try:
            slot_manager.conn.close()
        except Exception:
            pass

    return checks


# --------------------------------------------------------------------------
# Parent: kill loop and reporting.
# --------------------------------------------------------------------------


def run_kill_cycle(
    root: Path,
    seed: int,
    min_run_s: float,
    max_run_s: float,
) -> dict[str, Any]:
    """Spawn the worker, SIGKILL it mid-write, and verify what survived."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(root), str(seed)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    delay = random.uniform(min_run_s, max_run_s)
    time.sleep(delay)

    killed_while_running = proc.poll() is None
    if killed_while_running:
        os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)

    stderr = (proc.stderr.read() or b"").decode("utf-8", "replace") if proc.stderr else ""
    checks = verify_state(root)
    return {
        "seed": seed,
        "ran_for_s": round(delay, 3),
        "killed_while_running": killed_while_running,
        "signal": -proc.returncode if proc.returncode is not None and proc.returncode < 0 else None,
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "worker_stderr": stderr[-400:] if stderr and not killed_while_running else "",
    }


def build_report(cycles: list[dict[str, Any]], iterations: int, seed: int) -> dict[str, Any]:
    failed = [c for c in cycles if not c["passed"]]
    effective = [c for c in cycles if c["killed_while_running"]]

    by_check: dict[str, dict[str, int]] = {}
    for cycle in cycles:
        for check in cycle["checks"]:
            bucket = by_check.setdefault(check["check"], {"passed": 0, "failed": 0})
            bucket["passed" if check["passed"] else "failed"] += 1

    invariants = [
        {
            "name": "no_corrupt_state_after_sigkill",
            "passed": not failed,
            "detail": f"{len(failed)} of {len(cycles)} kill cycles left inconsistent state",
        },
        {
            "name": "kills_landed_mid_write",
            # A cycle where the worker already died proves nothing about atomicity.
            "passed": len(effective) == len(cycles),
            "detail": f"{len(effective)}/{len(cycles)} kills hit a live worker",
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "config": {"iterations": iterations, "seed": seed},
        "totals": {
            "cycles": len(cycles),
            "kills_on_live_worker": len(effective),
            "failed_cycles": len(failed),
        },
        "checks": by_check,
        "invariants": invariants,
        "failures": failed[:10],
        "passed": all(inv["passed"] for inv in invariants),
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    t = report["totals"]
    lines = [
        "# TEMMS Crash-Atomicity Report",
        "",
        f"_Generated {report['generated_at']} · schema `{report['schema_version']}`_",
        "",
        f"**Result: {'PASS ✅' if report['passed'] else 'FAIL ❌'}** "
        f"({t['cycles']} SIGKILL cycles, seed {report['config']['seed']})",
        "",
        "Each cycle spawns a real subprocess writing DDIL state, kills it with "
        "`SIGKILL` mid-write, and verifies what survived on disk.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Kill cycles | {t['cycles']} |",
        f"| Kills on a live worker | {t['kills_on_live_worker']} |",
        f"| Cycles leaving bad state | {t['failed_cycles']} |",
        "",
        "## Checks",
        "",
        "| Check | Passed | Failed |",
        "| --- | --- | --- |",
        *[
            f"| {name} | {counts['passed']} | {counts['failed']} |"
            for name, counts in sorted(report["checks"].items())
        ],
        "",
        "## Invariants",
        "",
        *[
            f"- {'✅' if inv['passed'] else '❌'} **{inv['name']}** — {inv['detail']}"
            for inv in report["invariants"]
        ],
        "",
    ]
    if report["failures"]:
        lines += ["## Failures", ""]
        for failure in report["failures"]:
            bad = [c for c in failure["checks"] if not c["passed"]]
            lines.append(
                f"- seed {failure['seed']}, ran {failure['ran_for_s']}s: "
                + "; ".join(f"{c['check']} ({c['detail']})" for c in bad)
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="TEMMS crash-atomicity soak")
    parser.add_argument("--worker", nargs=2, metavar=("ROOT", "SEED"), help=argparse.SUPPRESS)
    parser.add_argument("--iterations", type=int, default=25, help="Kill cycles to run")
    parser.add_argument("--seed", type=int, default=1729, help="Base RNG seed")
    parser.add_argument("--min-run", type=float, default=0.25, help="Min worker runtime (s)")
    parser.add_argument("--max-run", type=float, default=1.25, help="Max worker runtime (s)")
    parser.add_argument("--root", type=Path, help="State directory (default: a temp dir)")
    parser.add_argument("--report", type=Path, help="Write JSON report here")
    parser.add_argument("--markdown", type=Path, help="Write Markdown report here")
    args = parser.parse_args()

    if args.worker:
        run_worker(Path(args.worker[0]), int(args.worker[1]))
        return 0  # unreachable: the worker loops until killed

    random.seed(args.seed)
    if args.root:
        root = args.root
        root.mkdir(parents=True, exist_ok=True)
        cycles = _run_all(root, args)
    else:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="temms-crash-soak-") as tmp:
            cycles = _run_all(Path(tmp), args)

    report = build_report(cycles, args.iterations, args.seed)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report, args.markdown)

    t = report["totals"]
    status = "PASS" if report["passed"] else "FAIL"
    print(
        f"[{status}] {t['cycles']} SIGKILL cycles, "
        f"{t['kills_on_live_worker']} on a live worker, "
        f"{t['failed_cycles']} left inconsistent state"
    )
    for inv in report["invariants"]:
        print(f"  {'ok ' if inv['passed'] else 'FAIL'} {inv['name']}: {inv['detail']}")
    for failure in report["failures"]:
        for check in failure["checks"]:
            if not check["passed"]:
                print(f"  ! seed {failure['seed']}: {check['check']} — {check['detail']}")

    return 0 if report["passed"] else 1


def _run_all(root: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    cycles = []
    for index in range(args.iterations):
        cycles.append(
            run_kill_cycle(root, args.seed + index, args.min_run, args.max_run)
        )
    return cycles


if __name__ == "__main__":
    raise SystemExit(main())
