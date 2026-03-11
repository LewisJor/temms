"""
Visual simulation runner.

This is the main loop that ties everything together:
1. Reads frames from a video source (file, webcam, or generated)
2. Applies weather effects based on current scenario step
3. Sends augmented frames to TEMMS inference endpoint
4. Injects matching conditions into TEMMS condition store
5. Renders a live dashboard: video + TEMMS status + decision log

Run it:
    python -m temms.sim.runner --scenario fog_rollout
    python -m temms.sim.runner --scenario day_night_cycle --source webcam
    python -m temms.sim.runner --scenario fog_rollout --daemon-url http://localhost:8080

No webcam?  No problem.  Default source generates synthetic driving frames
using pure OpenCV (road + sky + horizon).
"""

import time
import sys
import logging
import argparse
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def generate_synthetic_frame(
    width: int = 640,
    height: int = 480,
    frame_idx: int = 0,
) -> np.ndarray:
    """
    Generate a synthetic outdoor scene using OpenCV primitives.

    Creates a simple landscape: sky gradient + ground plane + road markings.
    Cheap to compute, no external assets needed.
    """
    import cv2

    frame = np.zeros((height, width, 3), dtype=np.uint8)

    horizon = height // 2

    # Sky gradient (top = dark blue, horizon = light blue)
    for y in range(horizon):
        ratio = y / horizon
        b = int(180 + 75 * ratio)
        g = int(120 + 100 * ratio)
        r = int(50 + 80 * ratio)
        frame[y, :] = (b, g, r)

    # Ground (green/brown gradient)
    for y in range(horizon, height):
        ratio = (y - horizon) / (height - horizon)
        b = int(40 + 20 * ratio)
        g = int(100 + 40 * ratio)
        r = int(60 + 30 * ratio)
        frame[y, :] = (b, g, r)

    # Road (gray trapezoid converging to horizon)
    road_top_left = width // 2 - 30
    road_top_right = width // 2 + 30
    road_bottom_left = width // 4
    road_bottom_right = 3 * width // 4

    pts = np.array(
        [
            [road_top_left, horizon],
            [road_top_right, horizon],
            [road_bottom_right, height],
            [road_bottom_left, height],
        ],
        np.int32,
    )
    cv2.fillPoly(frame, [pts], (100, 100, 100))

    # Center line dashes (animated with frame_idx for motion feel)
    dash_offset = (frame_idx * 5) % 40
    for y in range(horizon + 10, height, 40):
        y_pos = y + dash_offset
        if y_pos >= height:
            continue
        ratio = (y_pos - horizon) / (height - horizon)
        cx = width // 2
        dash_len = int(8 + 12 * ratio)
        dash_width = max(1, int(2 * ratio))
        cv2.line(
            frame,
            (cx, y_pos),
            (cx, min(y_pos + dash_len, height - 1)),
            (240, 240, 240),
            dash_width,
        )

    # Add some "objects" — simple colored rectangles as vehicle proxies
    np.random.seed(frame_idx % 100)
    n_objects = np.random.randint(1, 4)
    for _ in range(n_objects):
        obj_y = np.random.randint(horizon + 20, height - 40)
        ratio = (obj_y - horizon) / (height - horizon)
        obj_size = int(10 + 30 * ratio)
        obj_x = np.random.randint(road_bottom_left + 20, road_bottom_right - 20)
        color = tuple(int(c) for c in np.random.randint(50, 220, 3))
        cv2.rectangle(
            frame,
            (obj_x - obj_size // 2, obj_y - obj_size // 3),
            (obj_x + obj_size // 2, obj_y + obj_size // 3),
            color,
            -1,
        )

    return frame


class SimRunner:
    """
    Main simulation loop.

    Drives weather augmentation + TEMMS condition injection + live rendering.
    """

    def __init__(
        self,
        daemon_url: str = "http://localhost:8080",
        source: str = "synthetic",  # "synthetic", "webcam", or path to video
        width: int = 640,
        height: int = 480,
        fps: float = 10.0,
        headless: bool = False,
    ):
        self.daemon_url = daemon_url.rstrip("/")
        self.source = source
        self.width = width
        self.height = height
        self.target_fps = fps
        self.headless = headless
        self._frame_idx = 0

        # State (updated by the render loop)
        self._active_model: str = "unknown"
        self._last_latency_ms: float = 0.0
        self._current_step_name: str = ""
        self._current_conditions: dict = {}
        self._decisions: list = []
        self._effects: dict = {}

    def _get_frame(self, cap=None) -> Optional[np.ndarray]:
        """Get next frame from source."""
        import cv2

        if self.source == "synthetic":
            return generate_synthetic_frame(self.width, self.height, self._frame_idx)
        elif self.source == "webcam":
            if cap is not None:
                ret, frame = cap.read()
                if ret:
                    return cv2.resize(frame, (self.width, self.height))
            return None
        else:
            if cap is not None:
                ret, frame = cap.read()
                if ret:
                    return cv2.resize(frame, (self.width, self.height))
                else:
                    # Loop video
                    cap.set(1, 0)  # cv2.CAP_PROP_POS_FRAMES
                    ret, frame = cap.read()
                    if ret:
                        return cv2.resize(frame, (self.width, self.height))
            return None

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

    def _send_frame_for_inference(self, frame: np.ndarray) -> dict:
        """Send a frame to the TEMMS inference endpoint."""
        import cv2

        try:
            import httpx

            _, buf = cv2.imencode(".jpg", frame)
            resp = httpx.post(
                f"{self.daemon_url}/v1/slots/vision/infer",
                files={"file": ("frame.jpg", buf.tobytes(), "image/jpeg")},
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug(f"Inference request failed: {e}")
        return {}

    def _render_dashboard(
        self,
        original: np.ndarray,
        augmented: np.ndarray,
    ) -> np.ndarray:
        """
        Compose the live dashboard frame:

        ┌──────────────────┬──────────────────┐
        │   Original       │   Augmented      │
        │   (clean)        │   (with weather) │
        ├──────────────────┴──────────────────┤
        │         Status Bar                   │
        │  Model: yolov8-lowlight  │ Step: fog │
        │  Latency: 12ms   │ FPS: 10          │
        └─────────────────────────────────────┘
        """
        import cv2

        h, w = original.shape[:2]

        # Side-by-side video panels
        top_row = np.hstack([original, augmented])

        # Status bar (dark background)
        bar_h = 120
        bar = np.zeros((bar_h, w * 2, 3), dtype=np.uint8)
        bar[:] = (30, 30, 30)

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.55
        white = (255, 255, 255)
        green = (0, 220, 100)
        yellow = (0, 220, 255)
        cyan = (220, 200, 50)

        # Labels on original and augmented panels
        cv2.putText(top_row, "ORIGINAL", (10, 25), font, 0.6, green, 2)
        cv2.putText(top_row, "AUGMENTED (TEMMS sim)", (w + 10, 25), font, 0.6, yellow, 2)

        # Status text
        y = 25
        cv2.putText(bar, f"Active Model: {self._active_model}", (15, y), font, font_scale, green, 1)
        cv2.putText(bar, f"Latency: {self._last_latency_ms:.0f}ms", (400, y), font, font_scale, white, 1)
        cv2.putText(bar, f"FPS: {self.target_fps:.0f}", (600, y), font, font_scale, white, 1)

        y = 55
        cv2.putText(bar, f"Scenario Step: {self._current_step_name}", (15, y), font, font_scale, cyan, 1)

        # Show active effects
        effects_str = "  ".join(
            f"{k}={v:.1f}" for k, v in self._effects.items() if v > 0.01
        )
        if not effects_str:
            effects_str = "none"
        cv2.putText(bar, f"Effects: {effects_str}", (400, y), font, font_scale, white, 1)

        y = 85
        # Show key conditions
        vis = self._current_conditions.get("environmental.atmospheric.visibility_m", "—")
        precip = self._current_conditions.get("environmental.atmospheric.precipitation", "—")
        ambient = self._current_conditions.get("environmental.celestial.ambient", "—")
        cv2.putText(bar, f"Visibility: {vis}m  |  Precip: {precip}  |  Light: {ambient}", (15, y), font, 0.45, white, 1)

        # Instructions
        cv2.putText(bar, "Press 'q' to quit, 's' to skip step, 'p' to pause", (15, 110), font, 0.4, (150, 150, 150), 1)

        return np.vstack([top_row, bar])

    def run_scenario(self, scenario_name: str) -> None:
        """
        Run a named scenario with live visualization.

        This is the main entrypoint. It:
        1. Loads the scenario timeline
        2. Opens the video source
        3. Loops through frames, applying weather + sending to TEMMS
        4. Shows a live dashboard window (unless headless)
        """
        import cv2
        from temms.sim.scenarios import SCENARIOS
        from temms.sim.weather import conditions_to_effects, apply_weather

        if scenario_name not in SCENARIOS:
            available = ", ".join(SCENARIOS.keys())
            print(f"Unknown scenario: {scenario_name}")
            print(f"Available: {available}")
            sys.exit(1)

        scenario = SCENARIOS[scenario_name]
        print(f"\n{'='*60}")
        print(f"  TEMMS Visual Simulation")
        print(f"  Scenario: {scenario.name}")
        print(f"  {scenario.description}")
        print(f"{'='*60}\n")

        # Open video source
        cap = None
        if self.source == "webcam":
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                print("Cannot open webcam, falling back to synthetic frames")
                self.source = "synthetic"
        elif self.source != "synthetic":
            cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                print(f"Cannot open video: {self.source}, falling back to synthetic frames")
                self.source = "synthetic"

        paused = False
        frame_time = 1.0 / self.target_fps

        try:
            while True:  # Support scenario.loop
                for step_idx, step in enumerate(scenario.steps):
                    self._current_step_name = step.name
                    self._current_conditions.update(step.conditions)

                    # Convert conditions to visual effects
                    self._effects = conditions_to_effects(self._current_conditions)

                    print(f"\n  [{step_idx + 1}/{len(scenario.steps)}] {step.name}")
                    if step.description:
                        print(f"  {step.description}")
                    print(f"  Duration: {step.duration_s}s  Effects: {self._effects}")

                    # Inject conditions into TEMMS daemon
                    self._inject_conditions(step.conditions)

                    # Hold this step for its duration
                    step_start = time.time()
                    skip_step = False

                    while time.time() - step_start < step.duration_s and not skip_step:
                        tick_start = time.time()

                        if not paused:
                            # Get frame
                            frame = self._get_frame(cap)
                            if frame is None:
                                frame = generate_synthetic_frame(
                                    self.width, self.height, self._frame_idx
                                )

                            # Apply weather effects
                            augmented = apply_weather(frame, self._effects, seed=self._frame_idx)

                            # Send to TEMMS for inference (non-blocking best-effort)
                            result = self._send_frame_for_inference(augmented)
                            if result:
                                self._active_model = result.get("model", self._active_model)
                                self._last_latency_ms = result.get("latency_ms", 0)

                            # Also poll daemon status periodically
                            if self._frame_idx % 10 == 0:
                                status = self._get_daemon_status()
                                if status:
                                    slots = status.get("slots", {})
                                    vision = slots.get("vision", {})
                                    if vision.get("active_model"):
                                        self._active_model = vision["active_model"]

                            self._frame_idx += 1

                            # Render dashboard
                            if not self.headless:
                                dashboard = self._render_dashboard(frame, augmented)
                                cv2.imshow("TEMMS Simulation", dashboard)

                        # Handle keyboard
                        if not self.headless:
                            key = cv2.waitKey(1) & 0xFF
                            if key == ord("q"):
                                print("\n  Simulation stopped by user.")
                                return
                            elif key == ord("s"):
                                skip_step = True
                            elif key == ord("p"):
                                paused = not paused
                                print(f"  {'PAUSED' if paused else 'RESUMED'}")

                        # Frame rate control
                        elapsed = time.time() - tick_start
                        sleep_time = frame_time - elapsed
                        if sleep_time > 0:
                            time.sleep(sleep_time)

                if not scenario.loop:
                    break

        except KeyboardInterrupt:
            print("\n  Simulation interrupted.")
        finally:
            if cap is not None:
                cap.release()
            if not self.headless:
                cv2.destroyAllWindows()
            print(f"\n  Total frames processed: {self._frame_idx}")

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
        print(f"  TEMMS Headless Simulation")
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
                print(f"  → Daemon not responding")

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
        description="TEMMS Visual Simulation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Synthetic frames + fog scenario (default)
  python -m temms.sim.runner --scenario fog_rollout

  # Use webcam as source
  python -m temms.sim.runner --scenario day_night_cycle --source webcam

  # Use a video file
  python -m temms.sim.runner --scenario rainstorm --source ~/videos/driving.mp4

  # Headless mode (for Docker / CI)
  python -m temms.sim.runner --scenario fog_rollout --headless

  # Point to a running daemon
  python -m temms.sim.runner --scenario fog_rollout --daemon-url http://localhost:8080

Available scenarios:
  fog_rollout      Clear → fog → near-zero visibility → clears
  day_night_cycle  Daylight → sunset → night → dawn
  rainstorm        Clear → drizzle → downpour → clearing
  combined_stress  Multi-factor: fog + night + battery + thermal
        """,
    )
    parser.add_argument(
        "--scenario", "-s",
        default="fog_rollout",
        help="Scenario to run (default: fog_rollout)",
    )
    parser.add_argument(
        "--source",
        default="synthetic",
        help="Video source: 'synthetic', 'webcam', or path to video file",
    )
    parser.add_argument(
        "--daemon-url",
        default="http://localhost:8080",
        help="TEMMS daemon URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--width", type=int, default=640,
        help="Frame width (default: 640)",
    )
    parser.add_argument(
        "--height", type=int, default=480,
        help="Frame height (default: 480)",
    )
    parser.add_argument(
        "--fps", type=float, default=10.0,
        help="Target FPS (default: 10)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without GUI (text output only, for Docker/CI)",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    runner = SimRunner(
        daemon_url=args.daemon_url,
        source=args.source,
        width=args.width,
        height=args.height,
        fps=args.fps,
        headless=args.headless,
    )

    if args.headless:
        runner.run_headless_scenario(args.scenario)
    else:
        runner.run_scenario(args.scenario)


if __name__ == "__main__":
    main()
