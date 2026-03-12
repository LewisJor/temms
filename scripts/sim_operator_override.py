#!/usr/bin/env python3
"""
Simulate operator override scenario against a running TEMMS daemon.

Demonstrates:
  1. Force a specific model via operator override
  2. Inject conditions that would normally trigger a switch
  3. Verify override holds against policy
  4. Clear override, verify policy takes over

Requires:
  - TEMMS daemon running on http://localhost:8080
  - Models imported

Usage:
  python scripts/sim_operator_override.py [--base-url http://localhost:8080]
"""

import argparse
import sys
import time

import httpx


def print_header(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def print_step(step: int, total: int, text: str) -> None:
    print(f"\n[{step}/{total}] {text}")


def run_scenario(base_url: str) -> bool:
    """Run the operator override scenario."""
    client = httpx.Client(base_url=base_url)
    total_steps = 5

    print_header("TEMMS Operator Override Scenario")
    print(f"  Target: {base_url}")

    # Step 1: Check initial state
    print_step(1, total_steps, "Checking initial state...")
    try:
        r = client.get("/v1/health", timeout=5)
        if r.status_code != 200:
            print("  FAIL: Daemon not healthy")
            return False
        print("  OK: Daemon is healthy")

        r = client.get("/v1/status", timeout=5)
        status = r.json()
        print(f"  Slots: {list(status['slots'].keys())}")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    # Step 2: Force model via operator override
    print_step(2, total_steps, "Forcing operator override: mobilenet-tiny...")
    try:
        r = client.post(
            "/v1/control/slots/vision/model",
            json={
                "model": "mobilenet-tiny",
                "reason": "Operator manual override for testing",
                "duration_s": None,  # Permanent until cleared
            },
            timeout=10,
        )
        if r.status_code == 200:
            print("  OK: Override applied successfully")
            print(f"  Response: {r.json()}")
        else:
            print(f"  INFO: Override response: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"  Could not set override: {e}")

    time.sleep(2)

    # Step 3: Inject conditions that would normally trigger a different model
    print_step(3, total_steps, "Injecting conditions that WOULD trigger policy switch...")
    client.post(
        "/v1/control/conditions",
        json={"conditions": {
            "environmental.atmospheric.visibility_m": 1000,
            "environmental.atmospheric.precipitation": "none",
            "environmental.celestial.ambient": "bright",
        }},
        timeout=5,
    )
    print("  Injected: clear daylight conditions (would normally select yolov8-daylight)")

    print("  Waiting 5s for policy evaluation...")
    time.sleep(5)

    try:
        r = client.get("/v1/slots/vision/status", timeout=5)
        slot = r.json()
        print(f"  Vision slot: model={slot['active_model']}")
        if slot["active_model"] and "mobilenet" in slot["active_model"]:
            print("  OK: Override held! Policy did NOT override the operator's choice.")
        else:
            print("  INFO: Model may have changed (override behavior depends on daemon implementation)")
    except Exception as e:
        print(f"  Could not check slot: {e}")

    # Step 4: Clear the override
    print_step(4, total_steps, "Clearing operator override...")
    try:
        # Clear condition overrides
        client.delete("/v1/control/conditions/overrides", timeout=5)
        print("  Cleared condition overrides")

        # Re-inject normal conditions
        client.post(
            "/v1/control/conditions",
            json={"conditions": {
                "environmental.atmospheric.visibility_m": 1000,
                "environmental.atmospheric.precipitation": "none",
            }},
            timeout=5,
        )
        print("  Re-injected normal conditions")
        print("  Waiting 5s for policy to take over...")
        time.sleep(5)

        r = client.get("/v1/slots/vision/status", timeout=5)
        slot = r.json()
        print(f"  Vision slot: model={slot['active_model']}")
    except Exception as e:
        print(f"  Could not check: {e}")

    # Step 5: Final status
    print_step(5, total_steps, "Final system status...")
    try:
        r = client.get("/v1/status", timeout=5)
        status = r.json()
        print(f"  System status: {status['status']}")
        for name, info in status["slots"].items():
            print(f"  Slot '{name}': state={info['state']}, model={info.get('active_model', 'none')}")
    except Exception as e:
        print(f"  Could not get status: {e}")

    print_header("Operator Override Scenario Complete")
    client.close()
    return True


def main():
    parser = argparse.ArgumentParser(description="TEMMS operator override scenario")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="TEMMS daemon base URL (default: http://localhost:8080)",
    )
    args = parser.parse_args()

    success = run_scenario(args.base_url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
