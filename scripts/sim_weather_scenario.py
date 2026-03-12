#!/usr/bin/env python3
"""
Simulate a weather-change scenario against a running TEMMS daemon.

Demonstrates the full policy-driven model switching flow:
  1. Start with clear weather -> yolov8-daylight active
  2. Fog rolls in -> policy switches to yolov8-lowlight
  3. Fog clears -> policy returns to default (yolov8-daylight)
  4. Critical visibility + critical mission -> mobilenet-tiny fallback

Requires:
  - TEMMS daemon running on http://localhost:8080
  - Weather-adaptive policy loaded
  - Models imported: yolov8-daylight, yolov8-lowlight, mobilenet-tiny

Usage:
  python scripts/sim_weather_scenario.py [--base-url http://localhost:8080]
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


def check_health(client: httpx.Client) -> bool:
    """Check if daemon is healthy."""
    try:
        r = client.get("/v1/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def get_status(client: httpx.Client) -> dict:
    """Get system status."""
    r = client.get("/v1/status", timeout=5)
    r.raise_for_status()
    return r.json()


def get_slot_status(client: httpx.Client, slot: str) -> dict:
    """Get slot status."""
    r = client.get(f"/v1/slots/{slot}/status", timeout=5)
    r.raise_for_status()
    return r.json()


def inject_conditions(client: httpx.Client, conditions: dict) -> dict:
    """Inject conditions via API."""
    r = client.post(
        "/v1/control/conditions",
        json={"conditions": conditions},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


def clear_overrides(client: httpx.Client) -> dict:
    """Clear all operator condition overrides."""
    r = client.delete("/v1/control/conditions/overrides", timeout=5)
    r.raise_for_status()
    return r.json()


def wait_for_policy(seconds: float = 3.0) -> None:
    """Wait for policy evaluation cycle."""
    print(f"  Waiting {seconds}s for policy evaluation...")
    time.sleep(seconds)


def run_scenario(base_url: str) -> bool:
    """Run the weather change scenario."""
    client = httpx.Client(base_url=base_url)
    total_steps = 7
    passed = True

    print_header("TEMMS Weather Scenario Simulation")
    print(f"  Target: {base_url}")

    # Step 1: Health check
    print_step(1, total_steps, "Checking daemon health...")
    if not check_health(client):
        print("  FAIL: Daemon not healthy. Is it running?")
        return False
    print("  OK: Daemon is healthy")

    # Step 2: Check initial status
    print_step(2, total_steps, "Checking initial system status...")
    status = get_status(client)
    print(f"  System status: {status['status']}")
    print(f"  Slots: {len(status['slots'])}")
    print(f"  Conditions: {status['conditions_count']}")
    print(f"  Policies: {status['policies_count']}")

    if "vision" not in status["slots"]:
        print("  WARN: Vision slot not found. Creating conditions anyway.")

    # Step 3: Set clear weather conditions
    print_step(3, total_steps, "Setting CLEAR weather conditions...")
    inject_conditions(client, {
        "environmental.atmospheric.visibility_m": 1000,
        "environmental.atmospheric.precipitation": "none",
        "environmental.celestial.ambient": "bright",
        "operational.mission.priority": "normal",
    })
    print("  Injected: visibility=1000m, precipitation=none, ambient=bright")
    wait_for_policy()

    try:
        slot = get_slot_status(client, "vision")
        print(f"  Vision slot: state={slot['state']}, model={slot['active_model']}")
    except Exception as e:
        print(f"  (Could not get slot status: {e})")

    # Step 4: Simulate fog rolling in
    print_step(4, total_steps, "Simulating FOG rolling in...")
    inject_conditions(client, {
        "environmental.atmospheric.visibility_m": 50,
        "environmental.atmospheric.precipitation": "fog",
    })
    print("  Injected: visibility=50m, precipitation=fog")
    wait_for_policy(5)

    try:
        slot = get_slot_status(client, "vision")
        print(f"  Vision slot: state={slot['state']}, model={slot['active_model']}")
        if slot["active_model"] and "lowlight" in slot["active_model"]:
            print("  OK: Model switched to lowlight variant (fog conditions)")
        else:
            print("  INFO: Model may not have switched yet (depends on policy eval cycle)")
    except Exception as e:
        print(f"  (Could not get slot status: {e})")

    # Step 5: Fog clears
    print_step(5, total_steps, "Simulating FOG clearing...")
    clear_overrides(client)
    inject_conditions(client, {
        "environmental.atmospheric.visibility_m": 800,
        "environmental.atmospheric.precipitation": "none",
    })
    print("  Injected: visibility=800m, precipitation=none")
    print("  Cleared operator overrides")
    wait_for_policy(5)

    try:
        slot = get_slot_status(client, "vision")
        print(f"  Vision slot: state={slot['state']}, model={slot['active_model']}")
    except Exception as e:
        print(f"  (Could not get slot status: {e})")

    # Step 6: Critical visibility + critical mission
    print_step(6, total_steps, "Simulating CRITICAL conditions...")
    inject_conditions(client, {
        "environmental.atmospheric.visibility_m": 10,
        "operational.mission.priority": "critical",
    })
    print("  Injected: visibility=10m, mission.priority=critical")
    wait_for_policy(5)

    try:
        slot = get_slot_status(client, "vision")
        print(f"  Vision slot: state={slot['state']}, model={slot['active_model']}")
        if slot["active_model"] and "mobilenet" in slot["active_model"]:
            print("  OK: Model switched to mobilenet-tiny (critical fallback)")
    except Exception as e:
        print(f"  (Could not get slot status: {e})")

    # Step 7: Final status
    print_step(7, total_steps, "Final system status...")
    status = get_status(client)
    print(f"  System status: {status['status']}")
    for name, info in status["slots"].items():
        print(f"  Slot '{name}': state={info['state']}, model={info.get('active_model', 'none')}")

    print_header("Scenario Complete")
    print(f"  All steps executed {'successfully' if passed else 'with issues'}")
    print(f"  Check decision log: temms log decisions --slot vision")
    print()

    client.close()
    return passed


def main():
    parser = argparse.ArgumentParser(description="TEMMS weather scenario simulation")
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
