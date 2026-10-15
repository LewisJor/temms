# Live local ONNX model swapping

Run real image classification on the local CPU while a replayed battery signal
changes which model TEMMS selects. The terminal shows successful switches, a
deliberately failed replacement, continued serving, and application abstention.
No predictions are fabricated. This is a small integration demonstration, not an
accuracy or performance benchmark.

## Prepare once

From the repository root, with Python 3.11+ and your virtual environment active:

```bash
python -m pip install '.[onnx,demo]'
python examples/live_swap/assets.py
```

Preparation downloads about 61 MB of pinned model files, one photo, and the label
list into `.demo-assets/live-swap`. Sources, revisions, licenses, sizes, and SHA-256
checksums are recorded in [assets.json](assets.json). The `demo` extra adds Pillow
for image decoding and preprocessing; core TEMMS still has no required runtime
dependencies. Keep downloaded weights and run records out of Git.

Serving prepared models needs only the `onnx,demo` extras. The `dev` extra adds
graph-building and test tools; it is not required on the device running this demo.

### Disconnected and intermittent connectivity

Prepare dependencies and assets before disconnecting. On a connected machine
matching the device's operating system, CPU architecture and Python version,
build a directory of installable wheels:

```bash
python -m pip wheel --wheel-dir /tmp/temms-wheels '.[onnx,demo]'
python examples/live_swap/assets.py --assets /tmp/temms-assets
```

Copy those two directories and `examples/live_swap` to the device. In its fresh
virtual environment, install using only the transferred wheels and run locally
(adjust paths to the copied directories):

```bash
python -m pip install --no-index --find-links /tmp/temms-wheels 'temms[onnx,demo]==0.2.0a1'
python examples/live_swap/run.py --assets /tmp/temms-assets
```

Use a fresh wheel directory when preparing a changed build of the same alpha
version. The example files are shipped in the source distribution, not the wheel.
Prepared inference and swapping need no upstream service. Missing or corrupt assets
fail explicitly; the runner does not download replacements.

If preparation is interrupted, rerun the preparation command when connectivity
returns. Complete files with matching sizes and hashes are reused; only missing
or invalid files are downloaded. An interrupted file starts again from the beginning.
Staging keeps partial downloads from replacing existing files, and a completed
preparation can be repeated offline. There is no automatic retry loop, byte-range
resume or synchronization service. This supports local operation under unavailable
or intermittent connectivity; it does not establish a bandwidth or recovery-time
guarantee for another device.

## Run and inspect

```bash
python examples/live_swap/run.py
```

The runner uses prepared local files and does not download assets. If preparation
is missing or an asset fails validation, follow the reported preparation command.
On the development Mac, the full replay passed inside an OS sandbox denying all
network operations, after an outbound TCP probe was confirmed denied. This verifies
that prepared assets suffice for this run; it is not a claim about another device.
The [hosted Ubuntu qualification](#ubuntu-2404-qualification-in-github-actions)
also passed under its separately recorded profile and pacing.

The defaults are six frame attempts per phase, with a 0.4-second interval. Very
short runs can finish their attempts before a new model publishes; use the defaults
when presenting so each newly active model has time to serve. ONNX Runtime
uses the CPU provider with one intra-op and one inter-op thread per session.
To change the asset directory, pacing, or output location:

```bash
python examples/live_swap/assets.py --assets .demo-assets/live-swap
python examples/live_swap/run.py --assets .demo-assets/live-swap --frames 6 --interval 0.4 --records /tmp/temms-live-swap.jsonl
```

Use a fresh path for `--records`. Without it, the runner chooses a unique JSONL
filename under the asset directory. Each record is one event; terminal output
makes the same run easy to follow. Read the selected model, actual active model,
serving model, activation outcome, and request outcome separately. A request's
serving model comes from its inference result, even if another model becomes
active before that request finishes.

The application uses MobileNetV2 at battery levels of at least 10%. ResNet18 is
also feasible at 40% or above and has higher configured priority. These are
**simulated battery values and illustrative policy thresholds**. They neither
measure battery usage nor physically enforce resource limits. Higher priority
does not establish higher accuracy.

| Phase | Simulated battery | What to observe |
| --- | ---: | --- |
| Start | 20% | MobileNetV2 activates and classifies the photo. |
| Switch | 80% | ResNet18 loads and warms before becoming active. |
| Return | 20% | MobileNetV2 becomes active again. |
| Failed switch | 80% | A missing private ResNet18 copy causes activation to fail; MobileNetV2 remains active and serves. |
| Retry | 80% | Restoring the private copy allows ResNet18 to activate. |
| Abstain | 5% | Neither model is feasible; the application stops admitting requests while the active session remains present. |
| Recover | 20% | MobileNetV2 serves again. |

The final terminal summary marks each of these seven scenes `PASS` or `MISSING`.
It also writes a final JSONL `summary` event containing `complete`, the `scenes`
mapping, and the ordered `missing` list. Exit status is **0** when all seven scenes
have evidence and **1** when the replay ends with missing evidence. Increase
`--frames` or `--interval` and use a fresh record path if a scene is missing.

A switch counts only when publication is followed by an actual request served by
the selected model. Failure continuity requires a request admitted after the failed
replacement and served by the preserved model. Successful frame records include
`activation_at_admission`; the existing `activation` field describes status when
the result is reported. These can differ for a request spanning a switch. Abstention
requires no selected or serving model, and any successful frame in that phase
invalidates its evidence. A completed summary verifies these observations in this
replay, without establishing accuracy, timing or resource guarantees.

To check incomplete-run reporting deliberately:

```bash
python examples/live_swap/run.py --frames 1 --interval 0 --records /tmp/temms-short-demo.jsonl
```

Expect an incomplete summary and exit status 1: one immediate attempt per phase
does not show the newly activated models serving every required scene. Predictions
and timing remain real; the runner does not add attempts or artificial load delays
to make the summary pass. An unexpected error aborts the run and may leave no final
summary; inspect its terminal error and saved records.

If inference raises while a replacement is still loading, the replay cancels and
settles its transition coroutine before propagating the original error. An abandoned
load cannot later publish; its worker thread can still run to completion. During the
missing-artifact phase, cleanup restores the private copy before returning the error.
The bounded [cleanup tests](../../tests/test_live_swap_cleanup.py) verify both cases
with injected sessions. This does not cover process termination or a hung native call.

Verified locally on 2026-10-12 with the pinned real models and OS network denial:
normal pacing produced 33 predictions, 3 waits and 6 abstentions across 42 attempts,
confirmed all seven scenes, and exited 0. The short run produced 3 predictions,
3 waits and 1 abstention, confirmed only the abstention scene, and exited 1.
An independent audit reconstructed these verdicts from ordered records. The first
failure-phase result in each run had been admitted before failure; only the later
post-failure request counted as continuity evidence. These counts describe those
runs, not a timing guarantee for another device. Raw records, frozen sources and
fresh stdout-replay videos are retained in `.demo-assets/live-swap/demo-evidence-20261012/`.

The installed demo also passed on Python 3.12.15 with ONNX Runtime 1.31.0, NumPy
2.5.4 and Pillow 12.3.0 on the same Mac. Its separate default/short runs reproduced
the complete/incomplete verdicts under OS network denial; the full 196-test suite
passed with zero skips. Records and independent checks are retained in
`.demo-assets/live-swap/python312-20261012/`. Different dependency versions and runs
are kept separate; this is compatibility evidence, not a performance comparison.

The failure scenario changes only a private temporary model copy, preserving the
prepared originals. Serving may continue on the previous feasible model while a
replacement prepares. A native load can finish quickly, so a particular run may
show few or no intervening frames; the runner adds no artificial loading delay.

## Model and image provenance

The two trained ONNX artifacts use the same 1,000-class ImageNet label ordering.
The pinned revisions and file hashes in [assets.json](assets.json) are the exact
inputs for this example.

| Asset | Source | License |
| --- | --- | --- |
| MobileNetV2 ONNX | [Hugging Face model card](https://huggingface.co/onnxmodelzoo/mobilenetv2-7/blob/b055c14ebe95ca2df473547484e4d447867951ed/README.md), revision `b055c14` | Apache-2.0 |
| ResNet18 ONNX | [Hugging Face model card](https://huggingface.co/onnxmodelzoo/resnet18-v1-7/blob/e7cb849a949bdceba02356b8b923d53cc01108e1/README.md), revision `e7cb849` | Apache-2.0 |
| 1,000-class synset labels | [ONNX Model Zoo](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/classification/synset.txt), revision `c5cae0f` | [Apache-2.0](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/LICENSE) |
| Chelsea cat photo | [scikit-image attribution](https://github.com/scikit-image/scikit-image/blob/e8a42ba85aaf5fd9322ef9ca51bc21063b22fcae/skimage/data/_fetchers.py), revision `e8a42ba`; Stefan van der Walt | CC0 |

Both sessions receive `data` as float32 NCHW with shape `1 × 3 × 224 × 224`.
The [ONNX Zoo preprocessing](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/classification/imagenet_preprocess.py)
converts to RGB, resizes the shorter side to 256 pixels with the
longer side rounded to the nearest pixel using bilinear interpolation, and takes
a 224-pixel center crop. Pixel values are divided by 255, then normalized with
mean `(0.485, 0.456, 0.406)` and standard deviation `(0.229, 0.224, 0.225)`.
The same input tensor works across a swap; output indices use the shared synset
list. These details are application code, not TEMMS selection policy.

This replay repeatedly classifies **one photo**. It establishes an observable
integration path with real weights, not classification quality, comparative
benefit, energy savings, latency guarantees, or readiness for another device.
The separate [comparison protocol and results](COMPARISON.md) evaluate fixed and
adaptive policies on twenty development images and 140 fresh validation images,
with predeclared deadlines, full request records, and explicit interpretation limits.

Historical raw records and video snippets referenced here are local development
evidence and are not included in a clone. The commands above create your own records;
published qualification runs provide downloadable evidence through GitHub Actions.

## Ubuntu 24.04 qualification in GitHub Actions

On **2026-10-15**, [qualification run 38754444278](https://github.com/LewisJor/temms/actions/runs/38754444278)
passed for published commit
[`58936d50756b1ed96fbc8d46f716f74194ee5ce5`](https://github.com/LewisJor/temms/commit/58936d50756b1ed96fbc8d46f716f74194ee5ce5).
It recorded **Ubuntu 24.04.5 LTS x86_64, CPython 3.12.15, ONNX Runtime 1.24.1,
NumPy 2.4.2 and Pillow 12.1.0**, using the CPU provider. ONNX 1.21.0 supplied the
synthetic native test fixtures. All **196 tests passed with zero skips**, including
both real ONNX tests. Independent inspection matched the installed wheel's nine
Python modules to that commit. The [progress record](../../docs/progress.md) also
links the successful general CI and core-only package runs.

The hosted replay recorded **84 frame attempts: 69 predictions, three waiting
frames and 12 abstentions**, with all seven scenes complete. Independent inspection
of ordered raw events and admission state identified these witnesses:

| Phase | Predictions | Waiting | Abstentions | Witness request ID |
| --- | ---: | ---: | ---: | ---: |
| Start | 11 | 1 | 0 | 2 |
| Switch | 12 | 0 | 0 | 14 |
| Return | 11 | 1 | 0 | 26 |
| Failed switch | 12 | 0 | 0 | 38 |
| Retry | 12 | 0 | 0 | 50 |
| Abstain | 0 | 0 | 12 | 61 |
| Recover | 11 | 1 | 0 | 74 |

Predictions during replacement can still belong to the previous feasible model.
Successful-switch witnesses were admitted after publication; the failed-switch
witness was admitted after failure and served the preserved MobileNetV2 session.
The replay and direct-IP probe shared a new network namespace as UID 1001, with
only loopback and no routes. The probe returned `ENETUNREACH` (101). Asset hashes
were unchanged after the replay.

The maintained [qualification workflow](../../.github/workflows/ubuntu24.yml)
builds a wheel on a push or pull request and installs it into a fresh environment.
The full test suite runs against that installed package;
missing native coverage or any skipped test fails qualification. Preparation downloads
and verifies the same pinned assets used above. The replay then runs as the runner
user inside a new Linux network namespace, with no external interface. A direct-IP
probe must fail with the kernel's no-route error before the replay starts. There is
no fallback to connected execution if isolation fails.

The qualification pacing is declared as `--frames 12 --interval 0.5`: 84 attempts
across seven phases, with six seconds of pacing per phase. All seven scenes must
complete in one run. Incomplete evidence, an unexpected exception, a test failure
or the job timeout fails the check; the workflow does not extend the run until it
passes. This is functional compatibility and offline-operation evidence, not a
deadline, accuracy, peak-memory or endurance qualification.

In the repository's **Actions** tab, open **Ubuntu 24.04 CPU qualification**. Inspect
the test and offline replay steps and download `ubuntu24-qualification-<run>-<attempt>`.
The artifact contains the wheel, its SHA-256, installed-module hashes, runner/OS/CPU
details, resolved dependency versions, test report, preparation log, network probe,
raw replay records and terminal output. Upload is attempted after failed steps;
hard cancellation or runner loss can prevent it. An uploaded artifact alone is not
a pass. The verified run's artifact is `ubuntu24-qualification-38754444278-1`.
Retention is 30 days, so preserve accepted run evidence with your release records.
Manual dispatch is also available after the
workflow exists on the default branch.

Top-level inference versions and Action revisions are pinned; the hosted image,
build tools and transitive dependencies can still change. Their actual versions are
recorded for each run. Do not describe this profile as a fully locked or bit-identical
build. The workflow checks installed imports explicitly; the live-swap script needs
its adjacent `assets` module, so it must not be invoked with Python's `-I` flag.

General CI, the core-only package check and Ubuntu qualification explicitly use
`ubuntu-24.04`. The qualification workflow also verifies the OS
release at execution time. GitHub updates the image behind that label, so its actual
image version is recorded with each run; the target does not silently follow
`ubuntu-latest`. See [GitHub's Ubuntu 24.04 image](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md).

## Local memory probes

To inspect session retirement and memory across thirty consecutive replacements:

```bash
python examples/live_swap/churn.py --strategy swapping --records /tmp/temms-churn.jsonl
```

Use a fresh record path. The same runner accepts `fixed-small` and `fixed-large`
for comparison. See the [memory protocol](COMPARISON.md#session-lifetime-and-swap-churn)
for repetitions, measurements, and the distinction between session lifetime and
process memory. This is a serial lifetime experiment, separate from the paced demo.
Add `--backend direct` to compare with the same native ONNX loading/inference without
TEMMS or its adapter. The [matched-control protocol](COMPARISON.md#matched-direct-onnx-control--declared-2026-10-12)
defines fresh paired runs and their interpretation limits.

To reproduce the longer CPU-arena comparison, use fresh files for both settings:

```bash
python examples/live_swap/churn.py --strategy swapping --cpu-arena on --steps 301 --records /tmp/arena-on.jsonl
python examples/live_swap/churn.py --strategy swapping --cpu-arena off --steps 301 --records /tmp/arena-off.jsonl
```

These options belong to `churn.py`; the terminal replay keeps its existing settings.
Defaults remain 31 steps and arena on. The runner records construction, warmup,
reconciliation, inference and full-step durations alongside memory. The three paired
local TEMMS repetitions on 2026-10-12 showed lower peaks with arena off, but one had
higher final RSS; the predeclared improvement criterion failed. See the
[arena protocol and results](COMPARISON.md#cpu-arena-result--2026-10-12) before interpreting
these service durations or using this native option in an application.
