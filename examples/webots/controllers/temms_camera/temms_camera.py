"""Webots camera fixture for TEMMS; simulator dependencies stay in this example."""

import asyncio
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from PIL import Image

# Webots starts controllers in their own directory. Import examples, not src/temms.
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from examples.live_swap import run, workload  # noqa: E402
from examples.live_swap.assets import preprocess_image, verify_assets  # noqa: E402
from temms import BestFeasibleSelector, Constraint, ModelPolicy, ModelRef, Operator  # noqa: E402
from temms.adapters import OnnxModel, OnnxRuntime  # noqa: E402

STEP = 32


def step(supervisor):
    if supervisor.step(STEP) == -1:
        raise RuntimeError("Webots stopped before the demo completed")


def camera_image(camera):
    """Own the BGRA bytes before another step can replace Webots' camera buffer."""
    data = camera.getImage()
    width, height = camera.getWidth(), camera.getHeight()
    if data is None or width < 1 or height < 1 or len(data) != width * height * 4:
        raise ValueError("Webots camera returned an incomplete BGRA image")
    return Image.frombytes("RGBA", (width, height), bytes(data), "raw", "BGRA").convert("RGB")


def rgb_digest(image):
    return hashlib.sha256(image.tobytes()).hexdigest()


def captured_feed(image, expected_digest):
    if rgb_digest(image) != expected_digest:
        raise ValueError("camera pixels changed from the frozen reference capture")
    feed = preprocess_image(image)
    for value in feed.values():
        value.setflags(write=False)
    return feed


def render(supervisor, camera, assets, item, yaw):
    texture = supervisor.getFromDef("CARD_TEXTURE").getField("url")
    path = str((assets / item["name"]).resolve())
    if texture.getCount():
        texture.setMFString(0, path)
    else:
        texture.insertMFString(0, path)
    supervisor.getFromDef("CARD").getField("rotation").setSFRotation([0, 0, 1, yaw])
    # One step applies scene edits; the next supplies the updated camera buffer.
    step(supervisor)
    step(supervisor)
    return camera_image(camera)


def prepare_camera(supervisor, camera, assets, output, data, refs, labels):
    (output / "inputs").mkdir()
    samples = []
    for index, item in enumerate(data["files"]):
        yaw = (index % 5 - 2) * 0.05
        image = render(supervisor, camera, assets, item, yaw)
        digest = rgb_digest(image)
        name = f"inputs/camera-{index + 1:03}.png"
        image.save(output / name)
        samples.append({
            "image": {"name": name,
                      "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()},
            "source": item, "yaw": yaw, "camera_rgb_sha256": digest,
            "expected": {"index": item["expected_index"], "label": labels[item["expected_index"]]},
            "feed": captured_feed(image, digest), "references": {},
        })
    if len({sample["camera_rgb_sha256"] for sample in samples}) != len(samples):
        raise ValueError("camera did not capture a distinct image for every source")
    return run.reference_inputs(samples, assets, refs)


async def simulation_pump(supervisor):
    while True:
        step(supervisor)
        await asyncio.sleep(STEP / 1000)


async def replay_with_steps(supervisor, *args, **kwargs):
    # A stopped simulator cancels replay immediately, including its fault cleanup.
    async with asyncio.TaskGroup() as tasks:
        pump = tasks.create_task(simulation_pump(supervisor))
        try:
            summary = await run.replay(*args, **kwargs)
        finally:
            pump.cancel()
    return summary


def finish_movie(supervisor, path, timeout=60):
    supervisor.movieStopRecording()
    deadline = time.monotonic() + timeout
    while not supervisor.movieIsReady():
        if time.monotonic() >= deadline:
            raise TimeoutError("Webots movie encoding did not finish")
        step(supervisor)
        time.sleep(0.01)
    if supervisor.movieFailed() or not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Webots failed to encode the demo movie")


def overlay(supervisor, record):
    event = record["event"]
    if event == "stage":
        selected = record["selected"]
        text = (f"{record['stage'].replace('-', ' ').upper()}\n"
                f"Battery {record['battery']}% (simulated) | "
                f"selected: {selected['id'] if selected else 'none'}")
        supervisor.setLabel(1, text, 0.025, 0.09, 0.045, 0xFFFFFF)
    elif event == "frame":
        serving = record["serving"]
        text = f"Frame {record['request']:02} | {record['outcome']}"
        text += f" | serving: {serving['id'] if serving else 'none'}"
        if record["outcome"] == "ok":
            text += (f"\nPrediction: {record['prediction']['label']}"
                     f"\nExpected: {record['expected']['label']}"
                     f"\nOutput reference: {'PASS' if record['reference_match'] else 'FAIL'}"
                     f" | attribution: {'PASS' if record['identity_match'] else 'FAIL'}")
        supervisor.setLabel(2, text, 0.025, 0.79, 0.04, 0x8FE3D2)
    elif event == "summary":
        text = "7 / 7 SCENES VERIFIED" if record["complete"] else "INCOMPLETE EVIDENCE"
        supervisor.setLabel(1, text, 0.025, 0.09, 0.05, 0x8FE3D2)


def run_header(manifest, data, samples):
    import temms

    return {
        "event": "run", "fixture": "webots-camera",
        "webots": (Path(os.environ["WEBOTS_HOME"]) / "resources/version.txt").read_text().strip(),
        "python": platform.python_version(), "executable": sys.executable,
        "temms_path": temms.__file__, "platform": platform.platform(),
        "versions": {name: version(name) for name in ("onnxruntime", "numpy", "Pillow")},
        "provider": "CPUExecutionProvider", "threads": 1, "manifest": manifest,
        "source_workload": data, "camera": {"width": 256, "height": 256, "step_ms": STEP},
        "score_tolerance": run.SCORE_TOLERANCE,
        "inputs": [{k: sample[k] for k in
                    ("image", "source", "yaw", "camera_rgb_sha256", "expected")}
                   for sample in samples],
        "references": [{"model": {"id": model, "digest": digest}, "image": sample["image"],
                        "scores": scores[0].tolist()}
                       for sample in samples
                       for (model, digest), scores in sample["references"].items()],
    }


def demonstrate(supervisor, assets, output):
    manifest = verify_assets(assets)
    data = workload.verify_workload(assets)
    labels = [line.split(" ", 1)[1] for line in (assets / "labels.txt").read_text().splitlines()]
    if len(labels) != 1000 or len(data["files"]) != 20:
        raise ValueError("this fixture requires 1000 labels and the pinned 20-image workload")
    digests = {item["name"]: item["sha256"] for item in manifest["files"]}
    refs = [ModelRef(name, "sha256:" + digests[name + ".onnx"])
            for name in ("mobilenetv2", "resnet18")]
    camera = supervisor.getDevice("camera")
    camera.enable(STEP)
    supervisor.setLabel(0, "TEMMS | LIVE CAMERA CLASSIFICATION", 0.025, 0.025, 0.05, 0x8FE3D2)
    supervisor.setLabel(1, "Preparing 20 camera captures and 40 model references...",
                        0.025, 0.09, 0.04, 0xFFFFFF)
    samples = prepare_camera(supervisor, camera, assets, output, data, refs, labels)
    selector = BestFeasibleSelector([
        ModelPolicy(refs[0], 10, (Constraint("battery", Operator.GTE, 10),)),
        ModelPolicy(refs[1], 20, (Constraint("battery", Operator.GTE, 40),)),
    ])
    with tempfile.TemporaryDirectory(prefix="temms-webots-") as private:
        large = Path(private) / "resnet18.onnx"
        shutil.copyfile(assets / "resnet18.onnx", large)
        runtime = OnnxRuntime(models={"vision": [
            OnnxModel(refs[0], assets / "mobilenetv2.onnx",
                      ("CPUExecutionProvider",), samples[0]["feed"]),
            OnnxModel(refs[1], large, ("CPUExecutionProvider",), samples[0]["feed"]),
        ]}, session_factory=run.cpu_session)
        record_demo(supervisor, camera, assets, output, manifest, data, samples,
                    runtime, selector, labels, large)
    # Fault injection must not change the prepared source assets.
    verify_assets(assets)
    workload.verify_workload(assets)


def record_demo(supervisor, camera, assets, output, manifest, data, samples,
                runtime, selector, labels, large):
    started = time.perf_counter()
    latest = {}
    with (output / "events.jsonl").open("x") as log:
        log.write(json.dumps(run_header(manifest, data, samples)) + "\n")
        log.flush()

        def capture(sample, request):
            image = render(supervisor, camera, assets, sample["source"], sample["yaw"])
            feed = captured_feed(image, sample["camera_rgb_sha256"])
            latest.update(request=request, rgb_sha256=rgb_digest(image),
                          simulation_seconds=supervisor.getTime())
            return sample | {"feed": feed}

        def emit(record):
            record = record | {"simulation_seconds": supervisor.getTime(),
                               "wall_seconds": time.perf_counter() - started}
            if record["event"] == "frame":
                record["capture"] = dict(latest)
            log.write(json.dumps(record, allow_nan=False) + "\n")
            log.flush()
            run.show(record)
            overlay(supervisor, record)

        movie = output / "demo.mp4"
        supervisor.movieStartRecording(str(movie), 960, 640, 0, 85, 1, False)
        try:
            summary = asyncio.run(replay_with_steps(
                supervisor, runtime, selector, samples, labels, large, emit, capture=capture,
            ))
            # Hold the final evidence on screen briefly before finishing the movie.
            for _ in range(32):
                step(supervisor)
            supervisor.exportImage(str(output / "overview.png"), 100)
        finally:
            finish_movie(supervisor, movie)
        if not summary["complete"]:
            raise RuntimeError("Webots replay did not witness every required scene and input")


def main():
    from controller import Supervisor

    supervisor = Supervisor()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    default = ROOT / ".demo-assets/webots" / stamp
    output = Path(os.environ.get("TEMMS_WEBOTS_OUTPUT", default)).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
        assets = Path(os.environ.get("TEMMS_ASSETS", ROOT / ".demo-assets/live-swap")).resolve()
        demonstrate(supervisor, assets, output)
    except Exception:
        traceback.print_exc()
        supervisor.simulationQuit(1)
        return 1
    print(f"Webots evidence and movie: {output}", flush=True)
    if os.environ.get("TEMMS_WEBOTS_QUIT") == "1":
        supervisor.simulationQuit(0)
    else:
        supervisor.simulationSetMode(supervisor.SIMULATION_MODE_PAUSE)
        while supervisor.step(STEP) != -1:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
