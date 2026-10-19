# Watch model swaps through a Webots camera

This demo places twenty pinned dog photographs on a slightly turning card in a
Webots scene. A stationary 256 × 256 camera captures the exhibit, and real
MobileNetV2 and ResNet18 sessions classify those camera pixels. A simulated battery
signal drives seven stages: startup, two switches, a failed replacement, retry,
abstention and recovery. The world keeps stepping while inference runs.

The target environment is **Ubuntu 24.04 x86_64, Webots R2025a and Python 3.12**.
The first hosted run passed; check the [verification record](../../docs/progress.md)
for its evidence and exact revision. The development Mac has not run this
Webots demo. The existing [terminal replay](../live_swap/README.md) remains available
without installing a simulator. Webots is an example dependency; TEMMS core is unchanged.

## Install and prepare

Start from a [TEMMS checkout](../../README.md#install-and-try). On Ubuntu 24.04,
download the pinned [R2025a Debian package](https://github.com/cyberbotics/webots/releases/tag/R2025a)
and verify its SHA-256 before installing:

```bash
curl -fL --output /tmp/webots_2025a_amd64.deb \
  https://github.com/cyberbotics/webots/releases/download/R2025a/webots_2025a_amd64.deb
echo '6253d58c9b625a83ed7b62cd85a640fd0542d441c48d633a60932208b40b0657  /tmp/webots_2025a_amd64.deb' | sha256sum --check
sudo apt update
sudo apt install /tmp/webots_2025a_amd64.deb python3.12-venv ffmpeg
```

Use the native Debian package for this procedure. Cyberbotics documents its
[installation and runtime requirements](https://github.com/cyberbotics/webots/blob/R2025a/docs/guide/installation-procedure.md).
From the repository root, create or activate a Python 3.12 environment and prepare
the existing models and image workload:

```bash
python3.12 -m venv .venv-webots
source .venv-webots/bin/activate
python -m pip install '.[onnx,demo]'
python examples/live_swap/assets.py
python examples/live_swap/workload.py
```

Preparation downloads about 61 MB of shared model assets and 655 KB of images.
The controller verifies the pinned files and does not download replacements.
An interrupted preparation can be rerun; verified files are reused.

## Launch and watch

Launch Webots from the same activated terminal so its controller uses the virtual
environment's `python`. The controller's `runtime.ini` specifies `COMMAND=python`.
Use a fresh output directory for each run:

```bash
export TEMMS_ASSETS="$PWD/.demo-assets/live-swap"
export TEMMS_WEBOTS_OUTPUT="$PWD/.demo-assets/webots/first-run"
webots --mode=fast --stdout --stderr examples/webots/worlds/classification.wbt
```

Leave the simulation running through preflight and the seven stages. Preflight
captures all twenty camera views and computes forty direct ONNX reference outputs.
The replay then captures each input again and checks the resulting prediction.
After the replay and movie encoding finish, the GUI pauses for inspection.

| Stage | Simulated battery | What to watch |
| --- | ---: | --- |
| Start | 20% | MobileNetV2 activates and serves camera frames. |
| More capacity | 80% | ResNet18 prepares, then becomes the serving model. |
| Less capacity | 20% | MobileNetV2 returns. |
| Missing artifact | 80% | A private ResNet18 copy is hidden; replacement fails while MobileNetV2 keeps serving. |
| Retry | 80% | The private copy is restored and ResNet18 activates. |
| No feasible model | 5% | The application abstains despite retaining an active session. |
| Recover | 20% | MobileNetV2 serves again. |

Read the selected, admitted, active and serving models separately. A frame admitted
before publication can correctly finish on the previous model. Expected breed and
predicted class may differ: classification mistakes remain visible and do not by
themselves fail the integration check. The failure fixture preserves original assets.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `TEMMS_ASSETS` | Repository `.demo-assets/live-swap` | Absolute path to the prepared assets. |
| `TEMMS_WEBOTS_OUTPUT` | Unique `.demo-assets/webots/<UTC timestamp>` | Fresh directory for records and media. |
| `TEMMS_WEBOTS_QUIT` | `0` | Set to `1` to exit after completion, as CI does; otherwise leave the paused GUI open. |

## Inspect and verify the result

| Output | What to inspect |
| --- | --- |
| `demo.mp4` | Native Webots recording of the scene and model transitions. |
| `overview.png` | Final scene with the completion overlay. |
| `inputs/camera-001.png` through `camera-020.png` | Twenty preflight camera captures used to build direct references. |
| `events.jsonl` | Input hashes, full references and scores, model identities, timing and final verdict. |

Open `demo.mp4` first, then inspect the camera PNGs to see the actual classifier
inputs. Keep the records with the media so the demonstration remains auditable.

Run the validator from the repository root in the same environment:

```bash
python examples/webots/verify.py "$TEMMS_WEBOTS_OUTPUT"
```

The validator prints a compact JSON summary to the terminal.

A successful result requires all seven scenes and all twenty inputs to have checked
service. Each replay capture's RGB hash must match its preflight camera view. Every
served output must match the full 1,000-score direct reference for its input and model
(`rtol=1e-5`, `atol=1e-6`), and the serving identity must match admission. Records retain
the full scores and references; a matching top prediction alone is insufficient.
The validator checks the saved evidence rather than treating a movie as proof of success.

Wall time and simulation time are recorded separately. Webots continues stepping
while ONNX work is awaited, so simulated time is not inference latency. Reference
setup, software rendering and video encoding also consume real time. This run does
not establish deadlines or performance on a physical device.

## Run or download it in GitHub Actions

Open the repository's **Actions** tab and select **Webots camera qualification**. Use
**Run workflow** when available, or inspect a completed run for the revision you
want to evaluate. The [workflow](../../.github/workflows/webots.yml) targets Ubuntu
24.04 with the pinned Debian package, a virtual display (Xvfb) and software OpenGL.
It is configured to prepare the assets, run the simulator in a network namespace
with external networking denied, validate the records and upload the media and
diagnostic evidence. Loopback remains available for Webots' local communication.

Download `webots-camera-<run>-<attempt>` and extract it. Open `run/demo.mp4` and
pass the extracted `run` directory to `verify.py` to inspect the saved result locally.
The artifact also contains environment, renderer, network and video-decoding logs;
GitHub retains it for 30 days. Check the job conclusion and commit together: an uploaded artifact
from an incomplete or failed run is diagnostic evidence, not a passing qualification.
See [progress](../../docs/progress.md) for actual hosted results and the exact tested
revision; a previous pass does not qualify subsequent changes.

## Troubleshooting

- **Python or module import errors:** close Webots, activate `.venv-webots`, confirm
  `python -m pip show temms onnxruntime Pillow`, then launch Webots from that terminal.
  Starting it from a desktop icon can select another Python environment.
- **Missing or corrupt assets:** rerun both preparation commands in the connected
  environment. Check that `TEMMS_ASSETS` is an absolute path to that directory.
- **Output directory already exists:** choose a new `TEMMS_WEBOTS_OUTPUT`; retain
  the earlier run for diagnosis instead of mixing records.
- **Movie failure or timeout:** inspect controller stderr and the workflow log,
  confirm FFmpeg is available, and let encoding finish. Software rendering can be
  slow. A missing final verdict or failed validator is incomplete evidence; a few
  visible predictions do not make the run pass.
- **Camera hash or score mismatch:** retain the images and records. The check is
  intended to expose changed camera input, stale output or wrong-model attribution;
  changing tolerances to hide the mismatch would invalidate the evidence.

## Scope and attribution

This is a rendered photo exhibit with image classification. It does not demonstrate
robot navigation, object detection, measured battery drain or physical resource
enforcement. Direct references share preprocessing and ONNX Runtime with serving;
they check routing consistency rather than independently validating the engine.
The stated environment is one qualification target, not a compatibility matrix.

Models, labels and image attribution are retained in the existing
[provenance guide](../live_swap/README.md#model-and-image-provenance),
[asset manifest](../live_swap/assets.json) and [workload manifest](../live_swap/workload.json).
Oxford-IIIT Pet photographs are CC-BY-SA-4.0; attribution: Omkar M. Parkhi,
Andrea Vedaldi, Andrew Zisserman and C. V. Jawahar, *Cats and Dogs*, CVPR 2012.
The same twenty images were already used by the terminal replay and comparison;
this is not a new accuracy evaluation.
