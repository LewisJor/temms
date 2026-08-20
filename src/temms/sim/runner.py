"""
Headless simulation runner.

Steps a named scenario against a running TEMMS daemon: injects each step's
conditions through the control API, waits out the step, and prints the
daemon's model-selection response. This is the single-machine validation
path for DDIL behaviour before field hardware.

Run it:
    python -m temms.sim.runner --scenario fog_rollout
"""

import argparse
import logging
import sys
import time

logger = logging.getLogger(__name__)




class SimRunner:
    """Steps scenarios against a live daemon and reports its decisions."""

    def __init__(self, daemon_url: str = "http://localhost:8080"):
        self.daemon_url = daemon_url.rstrip("/")
        self._current_step_name: str = ""
        self._current_conditions: dict = {}


    def _inject_conditions(self, conditions: dict) -> None:
        """Push conditions to the TEMMS daemon via HTTP API."""
        try:
            import httpx

            resp = httpx.post(
                f"{self.daemon_url}/v1/control/conditions",
                json={"conditions": conditions},
                timeout=2.0,
            )
            if resp.status_code != 200:
                logger.warning(f"Condition inject failed: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Could not inject conditions (daemon down?): {e}")

    def _get_daemon_status(self) -> dict:
        """Fetch current status from the TEMMS daemon."""
        try:
            import httpx

            resp = httpx.get(f"{self.daemon_url}/v1/status", timeout=2.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}




    def run_headless_scenario(self, scenario_name: str) -> None:
        """
        Run scenario without any GUI — just inject conditions and poll status.

        Useful in Docker or CI environments where there's no display.
        Outputs a text-based live view to stdout.
        """
        from temms.sim.scenarios import SCENARIOS
        from temms.sim.weather import conditions_to_effects

        if scenario_name not in SCENARIOS:
            available = ", ".join(SCENARIOS.keys())
            print(f"Unknown scenario: {scenario_name}")
            print(f"Available: {available}")
            sys.exit(1)

        scenario = SCENARIOS[scenario_name]
        print(f"\n{'='*60}")
        print("  TEMMS Headless Simulation")
        print(f"  Scenario: {scenario.name}")
        print(f"  {scenario.description}")
        print(f"  Daemon: {self.daemon_url}")
        print(f"{'='*60}\n")

        for step_idx, step in enumerate(scenario.steps):
            self._current_step_name = step.name
            self._current_conditions.update(step.conditions)
            effects = conditions_to_effects(self._current_conditions)

            header = f"[{step_idx + 1}/{len(scenario.steps)}] {step.name}"
            print(f"\n  {header}")
            print(f"  {'─' * len(header)}")
            if step.description:
                print(f"  {step.description}")

            # Inject conditions
            print(f"  Injecting: {step.conditions}")
            self._inject_conditions(step.conditions)

            # Wait for policy evaluation
            time.sleep(min(2.0, step.duration_s / 2))

            # Poll status
            status = self._get_daemon_status()
            if status:
                slots = status.get("slots", {})
                vision = slots.get("vision", {})
                model = vision.get("active_model", "unknown")
                state = vision.get("state", "unknown")
                print(f"  → Active model: {model} ({state})")
            else:
                print("  → Daemon not responding")

            # Effects summary
            active_effects = {k: v for k, v in effects.items() if v > 0.01}
            if active_effects:
                print(f"  → Visual effects: {active_effects}")

            # Hold remaining duration
            remaining = step.duration_s - 2.0
            if remaining > 0:
                time.sleep(remaining)

        print(f"\n{'='*60}")
        print(f"  Scenario complete: {scenario.name}")
        print(f"{'='*60}\n")


def main():
    """CLI entrypoint for the simulation runner."""
    parser = argparse.ArgumentParser(
        description="TEMMS headless scenario runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available scenarios:
  fog_rollout      Clear -> fog -> near-zero visibility -> clears
  day_night_cycle  Daylight -> sunset -> night -> dawn
  rainstorm        Clear -> drizzle -> downpour -> clearing
  combined_stress  Multi-factor: fog + night + battery + thermal
        """,
    )
    parser.add_argument(
        "--scenario", "-s",
        default="fog_rollout",
        help="Scenario to run (default: fog_rollout)",
    )
    parser.add_argument(
        "--daemon-url",
        default="http://localhost:8080",
        help="TEMMS daemon URL (default: http://localhost:8080)",
    )
    # Accepted and ignored so existing invocations with --headless keep working:
    # headless is now the only mode.
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    SimRunner(daemon_url=args.daemon_url).run_headless_scenario(args.scenario)


if __name__ == "__main__":
    main()
