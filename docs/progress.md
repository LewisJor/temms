# TEMMS progress — updated 2026-10-19

TEMMS remains an alpha Python library for application-owned local inference. This
iteration starts from [`9017ff86`](https://github.com/LewisJor/temms/commit/9017ff86518bc1ae4b2d2ddb1c8e9a9c54e31918)
and adds one behavior: run the existing live-swap scenario on actual Webots camera
captures, with a reproducible Linux qualification and a recorded visual tutorial.
Core and adapters are unchanged.

## Kept improvement: a rendered camera path

**Hypothesis and acceptance:** the existing numerical checks should also detect
stale input or wrong-model output when a simulator supplies the image. A passing
run must capture twenty distinct camera views, witness all seven transitions,
check every served output and admitted identity, preserve the prepared assets,
and produce a decodable native movie. Simulator and encoding failures must fail
the run. A movie or a repeated top prediction alone is insufficient evidence.

The [Webots tutorial](../examples/webots/README.md) uses a stationary camera looking
at twenty pinned dog photographs on a slightly turning card. Real MobileNetV2 and
ResNet18 sessions classify the camera pixels. The application supplies simulated
battery conditions; TEMMS selects and reconciles through the existing runtime.
This demonstrates classification, swaps, failed replacement, retry, abstention and
recovery. It does not demonstrate navigation or measured battery drain.

Preflight saves the twenty rendered camera images and forty direct ONNX reference
vectors. Each subsequent capture copies Webots' BGRA buffer before another step,
checks its RGB digest against preflight, and supplies a freshly preprocessed tensor
to inference. Every successful output must match the full reference vector and the
model captured at admission. A separate simulation task continues stepping while
ONNX work is awaited. Records distinguish simulation time from wall time; this
paced, software-rendered fixture is not a deadline or edge performance benchmark.

The simulator, controller, evidence verifier and workflow stay outside the package.
The existing file-based replay and camera replay share preprocessing, reference
preparation and the same seven-scene protocol. No backend or public protocol was
added. Regression tests cover camera-buffer ownership, malformed captures, input
forwarding on every attempt, capture failure during a replacement or hidden-artifact
phase, cancellation settlement, early simulator shutdown, movie failures and exit status.

## Hosted Webots qualification

The polished implementation, [`2e903a31`](https://github.com/LewisJor/temms/commit/2e903a318de24dc29df1ea0db951a025199cd367),
passed [Webots qualification](https://github.com/LewisJor/temms/actions/runs/39270677739)
and the [installed-wheel Ubuntu qualification](https://github.com/LewisJor/temms/actions/runs/39270677735),
including **257 tests without skips**. The earlier implementation at `39dc33d6`
also passed [its first native run](https://github.com/LewisJor/temms/actions/runs/39269572012).
The camera run used **Ubuntu 24.04 x86_64, Python 3.12.15, Webots R2025a,
ONNX Runtime 1.24.1, NumPy 2.4.2 and Pillow 12.1.0**. Mesa llvmpipe supplied software
rendering under Xvfb. The installed wheel's modules were compared with source bytes.

The replay ran as UID 1001 in a distinct network namespace with only loopback
available. The external direct-IP probe returned `ENETUNREACH` (101). Results:

- **Seven scenes passed; 84 attempts:** 69 served, three waiting, 12 abstained.
- **69 full-score and admission checks:** zero output or identity mismatches.
- **20/20 camera inputs** served with verified results; **35/40 model/input pairs** exercised.
- Simulation time advanced between capture and completion for **all 69 served requests**.
- **56/69 expected classes correct**, descriptive only; misclassifications remain visible.
- Native H.264 movie: **960 × 640, 850 frames, 27.200 seconds**; FFmpeg decoded it completely.

Artifact `webots-camera-39270677739-1` contains the movie, overview, twenty camera PNGs,
full records/references, wheel, environment, renderer and network evidence. GitHub
retains artifacts for 30 days; a downloaded copy is preserved locally under
`.demo-assets/webots/ci-39270677739/`, outside the repository. Camera images and the
scene overview were visually inspected. These results identify the tested revision;
the workflow reruns on each push, and its conclusion and commit identify any later
qualification. The retained video includes readable stage, activation and serving
labels; the default camera overlay is hidden to prevent it obscuring those labels.

## Local checks and size

The refined implementation passed **257 tests with zero skips or warnings**, strict
mypy on ten files, scoped Ruff and the architecture budget on macOS ARM64. The source
and wheel built with the installed build requirements (`--no-isolation`). Source
distributions include the Webots world and controller configuration; the wheel
contains only the library. The strict public-documentation build passed with
Sphinx 9.1.0, MyST 5.1.0 and Furo 2025.12.19 in a fresh Python 3.13.3 environment.
Webots itself has not been executed on this development Mac.

Core remains **429 physical lines** across five modules, largest 178; exports 50,
adapters 229 and the tiny example 37. Required runtime dependencies remain zero.
The file replay gained a capture callback and extracted shared reference preparation;
all simulator-specific work lives under `examples/webots` and its qualification job.
The original 224-test baseline passed before this work. The independent verifier
accepted the first hosted artifact and rejected **21 deliberately corrupted copies**,
including wrong admission eligibility, reordered activation, stale camera hashes,
non-argmax score damage and forged coverage summaries. The original artifact was
preserved; sixteen focused verifier regressions are part of the 257-test suite.

## Earlier evidence and limits

The previous numerical-integrity change at
[`38e4a374`](https://github.com/LewisJor/temms/commit/38e4a374ecf893168f3945f5a5b81bbf6f45cce0)
passed [Ubuntu qualification](https://github.com/LewisJor/temms/actions/runs/38810391287),
[general CI](https://github.com/LewisJor/temms/actions/runs/38810391312), the
[core installation check](https://github.com/LewisJor/temms/actions/runs/38810391251)
and [documentation deployment](https://github.com/LewisJor/temms/actions/runs/38810391376).
It ran **224 tests without skips**, including all three native ONNX cases. The file
replay checked 69 served outputs, 20/20 inputs and 35/40 pairs with no numerical or
identity mismatch; 59/69 expected labels matched. Its input was the prepared JPEG,
whereas this tutorial classifies the rendered camera view. Those descriptive counts
are not a controlled accuracy comparison.

The still earlier [single-photo Ubuntu run](https://github.com/LewisJor/temms/actions/runs/38754444278)
at `58936d5` passed 196 tests and the two then-existing native cases. Its repeated
Chelsea prediction did not establish varied-input routing correctness. Git history
retains that implementation; the current tutorial does not use it as acceptance.

Direct references share preprocessing and ONNX Runtime with serving; agreement
checks integration consistency rather than independently validating either component.
The same twenty photographs have already appeared in the terminal replay and
comparison. They are not a fresh representative accuracy evaluation. The
[comparison results](../examples/live_swap/COMPARISON.md), wrapper-overhead findings
and rejected CPU-arena experiment remain dated measurements and were not rerun here.
Physical-device latency, memory, energy and endurance remain unqualified. See the
[release/support criteria](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** perform the owner's unaided clean-clone trial using
[README.md](../README.md) and the [Webots tutorial](../examples/webots/README.md),
recording the revision, environment and any friction.

Read `camera_image` in `examples/webots/controllers/temms_camera/temms_camera.py`,
`replay` in `examples/live_swap/run.py`, and `OnnxRuntime.infer` in
`src/temms/adapters/onnx.py`. The hardest invariant remains that a request finishes
with its captured model's output and identity even after another model publishes.
Exercise without AI: extend the controlled in-flight test so the old native request
fails after publication, and assert that the new model still serves afterward.
