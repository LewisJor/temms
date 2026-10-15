# Live local ONNX model swapping

Classify twenty pinned dog images on the local CPU while a replayed battery signal
changes which model TEMMS selects. Each served output is checked against direct
ONNX execution for that exact model and image. The terminal shows predictions,
successful switches, a failed replacement, continued serving and abstention.
This verifies request/output consistency through the transitions; it is not an
accuracy or performance benchmark.

## Prepare once

From the repository root, with Python 3.11+ and your virtual environment active:

```bash
python -m pip install '.[onnx,demo]'
python examples/live_swap/assets.py
python examples/live_swap/workload.py
```

Preparation downloads about 61 MB of shared model assets and 655 KB of evaluation
images into `.demo-assets/live-swap`. [assets.json](assets.json) pins the models,
labels and Chelsea photo used by the other experiments; [workload.json](workload.json)
pins the twenty replay images and their expected classes. Both manifests retain
source revisions, licenses, sizes and SHA-256 hashes. The `demo` extra adds Pillow;
core TEMMS still has no required runtime dependencies. Keep assets and records out of Git.

Serving prepared models needs only the `onnx,demo` extras. The `dev` extra adds
graph-building and test tools; it is not required on the device running this demo.

### Disconnected and intermittent connectivity

Prepare dependencies and assets before disconnecting. On a connected machine
matching the device's operating system, CPU architecture and Python version,
build a directory of installable wheels:

```bash
python -m pip wheel --wheel-dir /tmp/temms-wheels '.[onnx,demo]'
python examples/live_swap/assets.py --assets /tmp/temms-assets
python examples/live_swap/workload.py --assets /tmp/temms-assets
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
It first computes forty direct references: each of the twenty inputs on each model.
The replay then cycles through manifest order by global request number, advancing
on waiting and abstained attempts too. Each serving session warms on the first workload image.

The defaults are twelve attempts per phase with a 0.4-second interval: 84 attempts
over seven phases. Short runs can miss publication or input coverage. ONNX Runtime
uses the CPU provider with one intra-op and one inter-op thread per session.
To change the asset directory, pacing, or output location:

```bash
python examples/live_swap/assets.py --assets .demo-assets/live-swap
python examples/live_swap/workload.py --assets .demo-assets/live-swap
python examples/live_swap/run.py --assets .demo-assets/live-swap --frames 12 --interval 0.4 --records /tmp/temms-live-swap.jsonl
```

Use a fresh path for `--records`. Without it, the runner chooses a unique JSONL
filename under the asset directory. Each record is one event; terminal output
makes the same run easy to follow, showing the input, expected class, prediction,
reference/identity verdicts and inference time. Read selected, admitted, active and
serving model identities separately. A request's
serving model comes from its inference result, even if another model becomes
active before that request finishes.

The application uses MobileNetV2 at battery levels of at least 10%. ResNet18 is
also feasible at 40% or above and has higher configured priority. These are
**simulated battery values and illustrative policy thresholds**. They neither
measure battery usage nor physically enforce resource limits. Higher priority
does not establish higher accuracy.

| Phase | Simulated battery | What to observe |
| --- | ---: | --- |
| Start | 20% | MobileNetV2 activates and serves workload images. |
| Switch | 80% | ResNet18 loads and warms before becoming active. |
| Return | 20% | MobileNetV2 becomes active again. |
| Failed switch | 80% | A missing private ResNet18 copy causes activation to fail; MobileNetV2 remains active and serves. |
| Retry | 80% | Restoring the private copy allows ResNet18 to activate. |
| Abstain | 5% | Neither model is feasible; the application stops admitting requests while the active session remains present. |
| Recover | 20% | MobileNetV2 serves again. |

Completion requires all seven scenes, at least one checked request, successful
checked service of all twenty inputs, and **zero output or attribution mismatches**.
The final JSONL `summary` retains scene verdicts, request counts, integrity counts,
missing inputs and observed model/input-pair coverage. Forty prepared references
do not imply that all forty pairs were served. Exit status is **0** for complete
evidence and **1** otherwise. More frames/time can address missing coverage; they
cannot repair an output or attribution mismatch already recorded in that run.

Every served request must return exactly one finite `(1, 1000)` score tensor that
matches the reference keyed by its serving model ID/digest and input. Comparison
uses `abs(actual - reference) <= 1e-6 + 1e-5 * abs(reference)` for every score
(`atol=1e-6`, `rtol=1e-5`). Its serving identity must also match the model captured
at admission. This catches stale input or wrong-model scores even when top-1 labels
agree. Indistinguishable reference pairs are rejected before replay. Controlled
[integrity tests](../../tests/test_live_swap_integrity.py) exercise these failures,
and a [native in-flight test](../../tests/test_onnx_runtime_integration.py) checks
numerical output and attribution across publication.

The header saves all forty reference vectors; served records save all 1,000 scores,
input identities and verdicts, so an audit can recompute the checks. Expected-class
correctness is descriptive: a model may match its reference while predicting the
wrong breed. The oracle shares preprocessing and the native engine with serving;
it does not independently validate either. Reference generation happens before
replay. `infer_ms` measures the awaited inference call, excluding reference setup,
score checks and record output; these warmed, paced observations are not a latency benchmark.

A switch counts only when publication is followed by an actual request served by
the selected model. Failure continuity requires a request admitted after the failed
replacement and served by the preserved model. Successful frame records include
`activation_at_admission`; the existing `activation` field describes status when
the result is reported. These can differ for a request spanning a switch. Abstention
requires no selected or serving model, and any successful frame in that phase
invalidates its evidence. Any output or identity mismatch also invalidates its
phase permanently. These checks do not establish accuracy, timing or resource guarantees.

To check incomplete-run reporting deliberately:

```bash
python examples/live_swap/run.py --frames 1 --interval 0 --records /tmp/temms-short-demo.jsonl
```

Expect an incomplete summary and exit status 1: seven attempts cannot cover all
twenty inputs and may miss post-publication service. Predictions
and timing remain real; the runner does not add attempts or artificial load delays
to make the summary pass. An unexpected error aborts the run and may leave no final
summary; inspect its terminal error and saved records.

If inference raises while a replacement is still loading, the replay cancels and
settles its transition coroutine before propagating the original error. An abandoned
load cannot later publish; its worker thread can still run to completion. During the
missing-artifact phase, cleanup restores the private copy before returning the error.
The bounded [cleanup tests](../../tests/test_live_swap_cleanup.py) verify both cases
with injected sessions. This does not cover process termination or a hung native call.

### Historical single-photo runs

The following runs used the previous Chelsea-only replay, without the current
direct-score or twenty-input coverage checks. On 2026-10-12 under OS network denial,
six attempts per phase at 0.4-second pacing produced 33 predictions, 3 waits and
6 abstentions across 42 attempts,
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
[assets.json](assets.json) and [workload.json](workload.json) pin the model and input bytes.

| Asset | Source | License |
| --- | --- | --- |
| MobileNetV2 ONNX | [Hugging Face model card](https://huggingface.co/onnxmodelzoo/mobilenetv2-7/blob/b055c14ebe95ca2df473547484e4d447867951ed/README.md), revision `b055c14` | Apache-2.0 |
| ResNet18 ONNX | [Hugging Face model card](https://huggingface.co/onnxmodelzoo/resnet18-v1-7/blob/e7cb849a949bdceba02356b8b923d53cc01108e1/README.md), revision `e7cb849` | Apache-2.0 |
| 1,000-class synset labels | [ONNX Model Zoo](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/classification/synset.txt), revision `c5cae0f` | [Apache-2.0](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/LICENSE) |
| Chelsea cat photo | [scikit-image attribution](https://github.com/scikit-image/scikit-image/blob/e8a42ba85aaf5fd9322ef9ca51bc21063b22fcae/skimage/data/_fetchers.py), revision `e8a42ba`; Stefan van der Walt | CC0 |
| Twenty Oxford-IIIT Pet images | [Pinned dataset](https://huggingface.co/datasets/timm/oxford-iiit-pet/tree/089695c834a7deb60505b7cc506672db1c31a6aa), revision `089695c`; names and annotations in [workload.json](workload.json) | [CC-BY-SA-4.0](https://www.robots.ox.ac.uk/~vgg/data/pets/) |

Oxford dataset attribution: Omkar M. Parkhi, Andrea Vedaldi, Andrew Zisserman and
C. V. Jawahar, *Cats and Dogs*, CVPR 2012. Image copyright remains with original
owners. Prepared JPEGs are pinned Hugging Face viewer renditions. The selection
contains two test images per predeclared breed, chosen before inspecting predictions;
it is reused from the existing comparison workload, not a fresh accuracy evaluation.

Both sessions receive `data` as float32 NCHW with shape `1 × 3 × 224 × 224`.
The [ONNX Zoo preprocessing](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/classification/imagenet_preprocess.py)
converts to RGB, resizes the shorter side to 256 pixels with the
longer side rounded to the nearest pixel using bilinear interpolation, and takes
a 224-pixel center crop. Pixel values are divided by 255, then normalized with
mean `(0.485, 0.456, 0.406)` and standard deviation `(0.229, 0.224, 0.225)`.
The same input tensor works across a swap; output indices use the shared synset
list. These details are application code, not TEMMS selection policy.

This replay checks input/output consistency during model changes. It does not
establish classification quality, comparative benefit, energy savings, latency
guarantees or readiness for another device. Pretraining overlap is unknown.
The separate [comparison protocol and results](COMPARISON.md) evaluate fixed and
adaptive policies on twenty development images and 140 fresh validation images,
with predeclared deadlines, full request records, and explicit interpretation limits.

Historical raw records and video snippets referenced here are local development
evidence and are not included in a clone. The commands above create your own records;
published qualification runs provide downloadable evidence through GitHub Actions.

## Ubuntu 24.04 qualification in GitHub Actions

The twenty-input replay passed [qualification run 38810391287](https://github.com/LewisJor/temms/actions/runs/38810391287)
at [`38e4a374`](https://github.com/LewisJor/temms/commit/38e4a374ecf893168f3945f5a5b81bbf6f45cce0)
on 2026-10-15: **224 tests, zero skips; 69 served outputs checked with zero mismatches;
20/20 inputs and 35/40 model/input pairs covered**. All seven scenes passed under
verified network isolation. The [progress record](../../docs/progress.md) retains
the full environment, counts and artifact location.

The maintained [qualification workflow](../../.github/workflows/ubuntu24.yml)
targets **Ubuntu 24.04 x86_64, CPython 3.12.15, ONNX Runtime 1.24.1, NumPy 2.4.2
and Pillow 12.1.0**, with ONNX 1.21.0 for native test fixtures. It builds and installs
a wheel in a fresh environment. Missing required native cases or any skipped test
fails qualification. Preparation downloads and verifies both the shared assets and
twenty-image workload. The replay then runs as the runner
user inside a new Linux network namespace, with no external interface. A direct-IP
probe must fail with the kernel's no-route error before the replay starts. There is
no fallback to connected execution if isolation fails.

The qualification pacing is declared as `--frames 12 --interval 0.5`: 84 attempts
across seven phases, with six seconds of pacing per phase. All seven scenes and all
twenty served inputs must pass in one run. Every served frame's full scores and
admitted identity are checked; an output or attribution mismatch fails qualification.
Incomplete evidence, an exception, a test failure or the job timeout also fails it;
the workflow does not extend the run until it passes. The [progress record](../../docs/progress.md)
identifies the exact tested revision. These are functional and offline-operation
checks, not deadline, accuracy, peak-memory or endurance guarantees.

Historical baseline: on **2026-10-15**, [run 38754444278](https://github.com/LewisJor/temms/actions/runs/38754444278)
passed at [`58936d5`](https://github.com/LewisJor/temms/commit/58936d50756b1ed96fbc8d46f716f74194ee5ce5)
using the **previous single-photo replay**. It recorded Ubuntu 24.04.5 LTS,
196 tests with zero skips, and 84 attempts: 69 predictions, three waits and
12 abstentions across all seven scenes. An independent audit verified module bytes,
ordered scene witnesses and network isolation as UID 1001 with `ENETUNREACH` (101).
That result predates the current full-score and twenty-input coverage requirements.

In the repository's **Actions** tab, open **Ubuntu 24.04 CPU qualification**. Inspect
the test and offline replay steps and download `ubuntu24-qualification-<run>-<attempt>`.
The artifact contains the wheel, its SHA-256, installed-module hashes, runner/OS/CPU
details, resolved dependency versions, test report, preparation log, network probe,
raw replay records and terminal output. Upload is attempted after failed steps;
hard cancellation or runner loss can prevent it. An uploaded artifact alone is not
a pass. The historical baseline artifact is `ubuntu24-qualification-38754444278-1`.
Retention is 30 days, so preserve accepted run evidence with your release records.
Manual dispatch is also available after the
workflow exists on the default branch.

Top-level inference versions and Action revisions are pinned; the hosted image,
build tools and transitive dependencies can still change. Their actual versions are
recorded for each run. Do not describe this profile as a fully locked or bit-identical
build. The workflow checks installed imports explicitly; the live-swap script needs
its adjacent example modules, so it must not be invoked with Python's `-I` flag.

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
