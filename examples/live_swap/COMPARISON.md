# Local comparison protocol

The initial protocol compared fixed MobileNetV2, fixed ResNet18, and blocking
adaptive selection on one CPU. The follow-up adds background replacement after
the initial result exposed transition delays. Both use the same frozen workload,
policy, arrival rate, and deadline. This is a small reproducible experiment, not
evidence of general model quality or energy savings.

The dated results below summarize local development experiments. Their ignored
`.demo-assets` directories are not distributed with the repository. Use the commands
and pinned manifests here to generate independent results on your own machine;
the reported timings and small quality differences are not acceptance targets.

A separate [fresh-image validation](#fresh-image-validation-protocol--declared-2026-10-12)
uses 140 previously unused images with the same policy and trace timing.

## Prepare and run

From the repository root with the `onnx,demo` extras installed:

```bash
python examples/live_swap/assets.py
python examples/live_swap/workload.py
python examples/live_swap/compare.py
python examples/live_swap/overhead.py --records .demo-assets/live-swap/overhead.jsonl
```

The first two commands explicitly prepare files; the last two run offline.
Comparison takes roughly three minutes at the declared arrival rate, plus setup.
It creates a new timestamped directory with the protocol, twelve raw JSONL files,
and per-run `summary.json`. Use `--output PATH` to choose a fresh directory.
The overhead command also requires a fresh output path and must run separately
from the comparison to avoid CPU contention. `--help` describes supported options.

The extra images total about 655 KB. [workload.json](workload.json) pins the
dataset revision, annotations, bytes, and SHA-256 hashes. Oxford licenses the
[dataset](https://www.robots.ox.ac.uk/~vgg/data/pets/) under CC-BY-SA-4.0; attribution:
Omkar M. Parkhi, Andrea Vedaldi, Andrew Zisserman, and C. V. Jawahar, *Cats and Dogs*,
CVPR 2012. Image copyright remains with original owners. The prepared files are
Hugging Face viewer JPEG renditions and may differ from original image encodings.
Preparation checks the viewer's revision and annotations before resolving temporary
image URLs; if the upstream revision or encoding changes, it fails instead of
silently changing the experiment. Already verified files need no network.

## Predeclared workload and acceptance

- Twenty Oxford-IIIT Pet **test** images: the first two test rows for each of ten
  predeclared dog breeds with exact ImageNet synset equivalents. The pinned
  workload manifest records the selection rule, annotations, and hashes. Chelsea,
  the existing demo image, is used only for warmup. No thresholds or labels are
  tuned on evaluation predictions. Images were selected before the initial
  evaluation, excluded from warmup, and reused in this follow-up. Overlap with
  the pretrained models' training data is unknown.
- Seven phases from the terminal demonstration, each receiving every evaluation
  image in manifest order: 20%, 80%, 20%, 80% with the private ResNet file missing,
  80% after restoration, 5%, then 20%. Conditions are simulated.
- Requests arrive at **100 ms intervals**, with a **100 ms completion deadline**
  from scheduled arrival. This is an illustrative 10 Hz application target,
  chosen before measurement; it is not a claimed device requirement.
- One serial FIFO worker, no drops and no expiration cancellation. Backlogged
  requests retain their arrival-time context. This can be stale at service time;
  applications with live sensor requirements must choose their own admission rule.
  Record queue delay and late completions; never reset deadlines when service starts.
  `adaptive` blocks this worker during reconciliation. `adaptive-background` allows
  one replacement task to overlap inference when the active model remains feasible.
- Three fresh processes per strategy, rotating strategy order across repetitions.
  CPU provider, one intra-op and one inter-op thread, identical preprocessing,
  one Chelsea warmup on every session creation, no retained inactive-session cache.
  Every fresh worker has the same two-thread executor for adapter calls. Background
  replacement can therefore add concurrent native work and contention. With four
  strategies and three repetitions, rotation only partially counterbalances order.
  OS file caches are not flushed. There is no independent hardware replication.

All strategies use the same feasibility rules: MobileNetV2 requires at least 10%
and ResNet18 at least 40%. Fixed strategies have only their named policy; adaptive
uses both, with ResNet18 preferred when feasible. All strategies abstain if their
active model is infeasible. Fixed-large therefore abstains at low simulated battery.
That is a policy consequence, not a measured power saving or an accuracy advantage.

The same missing-file event applies to every strategy. Already resident sessions
remain usable; fixed-large must not reload solely to manufacture a failure. Adaptive
should fail replacement once, retain the small session, then succeed after restore.

Acceptance is **measurement correctness**, not an adaptive win: identical offered
requests, complete records for every outcome, no infeasible serving, correct
captured-model attribution, failed-swap continuity, unchanged original artifacts,
and zero unexpected errors. A negative comparison is a valid result. Report useful
correct responses by deadline on **all offered inputs** as the primary task metric.

### Background follow-up, declared before its measurements

Hypothesis: serving the feasible old model while an upgrade loads may reduce late
responses, at the cost of changing which model answers transition inputs and of
concurrent CPU/memory demand. Rerun all four strategies in fresh workers; do not pool
the follow-up with the earlier three-strategy measurements. The scheduling change
was informed by those results, so this is not independent confirmation.

At the first request of each phase, start exactly one reconciliation. Startup and
infeasible-active transitions wait for it, preserving the original arrival deadline.
During a feasible upgrade, requests continue on the captured active session. Each
phase drains its outstanding replacement before changing context or restoring the
private fault artifact. This wait may add queue delay to the next phase. No deadline,
label, threshold, preprocessing, warmup, or image order is retuned.

Cancellation or failed record output cancels the pending reconciliation coroutine.
Cancellation delivered before publication prevents late publication; it does not
roll back a session already published. An already-running native worker can still finish;
worker shutdown waits for it. This is not hard cancellation of native execution.

## Measurements and scope

Every request records its input ID, expected synset, scheduled arrival, queue delay,
condition, selected/active/serving identities, prediction, correctness, deadline
status, and full latency. Full latency includes waiting, image decoding/preprocessing,
any blocking reconciliation, loading/warmup waits, inference, and
output interpretation. Result serialization is outside individual request timing.
Abstentions and errors stay in denominators. Never average only successful steady
state requests to represent transition behavior.

`active` is sampled immediately before admission; `active_at_completion` is sampled
after output interpretation. `serving` always comes from the captured inference
result. A result may correctly name the old model while the completion snapshot
names its replacement. Activation records include start/completion timestamps from
the trace origin and may be written after a request record. `trace_and_drain_ms`
includes any replacement still pending after the final request; request throughput
continues to use the final request completion. Record output time can delay later
requests but is outside the emitting request's measured latency.

Report offered, served, correct, useful-before-deadline, abstained, dropped, errors,
and late counts; served-only accuracy separately; full-request p50/p95/max and sample
counts; throughput over the offered trace through its final completion; activation,
session-load and warmup durations, including failed reconciliation durations.
The complete raw JSONL and protocol/environment header are retained for every run.
Latency percentiles use nearest rank (the lower median for an even sample count).
The terminal table pools outcome counts across repetitions, shows the range of
per-run p95 latencies, and the maximum process RSS. All-request latencies include
abstentions; the JSON summaries also retain served-only latency distributions.

Peak memory is each fresh worker's `getrusage(RUSAGE_SELF).ru_maxrss`, including
imports, native allocators, file hashing, tensors and old/new session overlap.
It is a process high-water mark, not a model-only measurement or an exact overlap
size. Report bytes on macOS and convert Linux KiB to bytes; unsupported platforms
report unavailable. Do not subtract high-water marks or call Python allocation
tracking native model memory. Power/energy and hard real-time guarantees are not measured.

Wrapper overhead is a **separate paired experiment**: call `TEMMS.infer` and
`OnnxRuntime.infer` on the same already-active session/input, alternating pair order.
Retain signed per-pair differences. This excludes preprocessing, activation, policy
selection and request queueing; negative differences are measurement noise, not
evidence that a wrapper accelerates inference.

Method references: [ONNX Runtime threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html),
[Python monotonic performance timer](https://docs.python.org/3/library/time.html#time.perf_counter_ns),
[process resource accounting](https://docs.python.org/3/library/resource.html#resource.getrusage),
[Darwin getrusage](https://github.com/apple/darwin-xnu/blob/main/bsd/man/man2/getrusage.2),
and [Linux getrusage](https://man7.org/linux/man-pages/man2/getrusage.2.html).

## Initial blocking result — 2026-10-12

Apple M1 Pro (10 logical CPUs), macOS 26.6.2 arm64, Python 3.11.8,
ONNX Runtime 1.24.1, NumPy 2.4.2, Pillow 12.1.0. All nine workers completed
inside a macOS sandbox denying network operations; an outbound probe first
confirmed `EPERM`. Three repetitions per strategy, 140 offered requests per run:
these are repetitions of **20 unique images**, not 420 independent examples.

| Strategy | Served / offered | Correct | Correct by 100 ms | Abstained | Late | All-request p95 range | Peak process RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed MobileNetV2 | 360 / 420 | 306 | 306 | 60 | 0 | 28.6–30.5 ms | 118.7 MiB |
| Fixed ResNet18 | 180 / 420 | 162 | 157 | 240 | 5 | 54.1–59.1 ms | 201.7 MiB |
| Adaptive | 360 / 420 | 312 | 305 | 60 | 7 | 54.3–56.1 ms | 240.7 MiB |

Every run had zero inference errors and zero drops. Adaptive had exactly one
expected failed replacement per repetition, kept the small model serving, and
recovered after restoration. Fixed-large's already-resident session was unaffected
by the missing file. The raw trace audit verified all 1,260 offered input/context
pairs, scheduled deadlines, identity/feasibility, correctness, and intact originals.

Adaptive made six more correct predictions overall than fixed-small, but delivered
one fewer correct result before the deadline and used a larger process peak.
This small experiment gives no reason to prefer adaptive for this particular
serial workload. The one-result difference is not a statistically established
ranking. Fixed-large's low-battery abstentions follow the simulated policy, not
an observed power constraint. Served-only accuracies were 85%, 90%, and 86.7%.

Across all strategy runs, MobileNet session construction took 12.5–30.7 ms and its
warmup 15.4–22.1 ms (12 loads); ResNet construction took 32.0–62.6 ms and warmup
47.5–54.2 ms (9 loads). These are observed ranges, not portable latency bounds.
Per-request and reconciliation timing, throughput, and failed-attempt durations
remain in the raw files and summaries.

The separate overhead experiment used three fresh processes, each with 100 pairs
per model. Per-run median wrapped-minus-direct differences ranged from **−11.4 to
+14.9 microseconds** for MobileNet and **−4.0 to +54.8 microseconds** for ResNet.
The sign changed across repetitions: native execution/scheduling noise prevents a
precise wrapper-cost conclusion from these measurements. No speedup is claimed.

Local raw evidence is under `.demo-assets/live-swap/comparison-20261012/`:
`protocol.json`, nine strategy JSONL files, `summary.json`, and three overhead
JSONL files. `measurement-source.tar.gz` preserves the matching source snapshot.
The first raw headers truthfully retain stale editable distribution metadata
(`temms=0.1.0`); imports were verified to come from this checkout's current source,
whose package version is `0.2.0a1`. Reinstalling the editable package corrected that
metadata without changing model or kernel code. The built wheel's Python files
were checked byte-for-byte against that source. Do not rewrite historical records.

## Background follow-up result — 2026-10-12

All four strategies were rerun under the frozen follow-up protocol, with the same
hardware, operating system, Python and model-library versions listed above; package
metadata now correctly reports `temms=0.2.0a1`. All twelve fresh workers completed
with OS network denial and an `EPERM` outbound probe. No other trained-model
benchmark ran concurrently; background OS/development activity was not controlled.
These measurements are separate from the initial table.

| Strategy | Served / offered | Correct | Correct by 100 ms | Abstained | Late | All-request p95 range | Peak process RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed MobileNetV2 | 360 / 420 | 306 | 305 | 60 | 1 | 29.4–30.4 ms | 121.0 MiB |
| Fixed ResNet18 | 180 / 420 | 162 | 156 | 240 | 6 | 55.1–56.1 ms | 205.4 MiB |
| Blocking adaptive | 360 / 420 | 312 | 306 | 60 | 6 | 54.5–55.4 ms | 256.7 MiB |
| Background adaptive | 360 / 420 | 312 | 312 | 60 | 0 | 54.4–55.0 ms | 265.7 MiB |

Background replacement delivered six more correct responses by the deadline than
blocking adaptation, and seven more than fixed-small, on these repeated inputs.
Each background repetition delivered 104/140 useful responses with zero observed
late completions. Each adaptive variant preserved feasible serving through exactly
one injected replacement failure per repetition, then recovered. All twelve runs
had zero inference errors, unexpected activation errors, and drops.

An independent audit checked all **1,680 requests**, their timing and model identities,
summaries, source snapshots, frozen protocol, and unchanged assets. Background served
six upgrade requests on MobileNet while replacement was still pending, completing
them in 41.3–67.3 ms; the corresponding blocking requests took 124.3–142.2 ms.
All six were correct with either model. Pooled background serving used MobileNet
246 times and ResNet 114 times, versus 240/120 for blocking adaptation. This records
request/reconciliation overlap; it does not timestamp simultaneous native kernels.

The background process peak was higher than either comparison strategy. These are
separate process high-water marks, not a measurement of incremental model memory.
Concurrent loading changes which model serves transition inputs; equal aggregate
correct counts do not mean each input used the same model. Zero observed misses
is not a deadline guarantee. This tiny workload, repeated on one device with only
partial order counterbalancing, does not establish a portable performance ranking.

The follow-up raw protocol, twelve runs, summaries, isolation note, and matching
source archive are retained under `.demo-assets/live-swap/background-20261012/`.
The paired overhead experiment was not repeated: the TEMMS inference wrapper and
runtime adapter are unchanged. Earlier overhead limits still apply.

## Fresh-image validation protocol — declared 2026-10-12

The earlier twenty images informed development of background replacement. This
validation uses **140 different test images**, selected before inspecting their
predictions. It tests whether the observed task benefit persists without changing
the models, policy, preprocessing, warmup, arrival rate, deadline, phase duration,
executor, native thread settings, or strategy order. An adaptive win is not an
acceptance condition; the correctness gates above still apply.

```bash
python examples/live_swap/workload.py --workload validation
python examples/live_swap/compare.py --workload validation
```

The default `--workload small` continues to reproduce the earlier twenty-image
experiment. [validation.json](validation.json) freezes the new image IDs, row
annotations, hashes, order, and phase assignments. Selection takes the next fourteen
test rows per existing breed, in source-row order after excluding the original two.
Phase `p` uses fresh occurrence ranks `2p` and `2p+1` for every breed. Within each
rank, rotate the existing breed order left by `p` positions. This pre-prediction
ordering decision spreads phase-boundary requests across breeds.
The extra validation images total 4,819,230 bytes. Preparation verified that all
140 decode successfully, with no byte, decoded-pixel, or preprocessed-tensor hash
overlap with each other or the original twenty images. No predictions were used
to choose or replace inputs.

Each phase receives twenty unique images, two per breed. Each full run offers
140 unique inputs once, rather than repeating twenty inputs through seven phases.
Every strategy/repetition receives the same exact ordered input/context/arrival
trace. All scheduled arrivals remain `request_index × 100 ms`. The header records
the selected workload and complete manifest; the output folder retains its own
`workload.json` snapshot. Mixed, unknown, or incomplete phase assignments fail before
activation or inference rather than silently omitting inputs.

Three runs yield 420 attempts per strategy but only 140 unique images. Timings share
queueing, phase, and machine effects; pooled requests are not independent samples.
Fixed-large serves a different subset of images from fixed-small, so served-only
accuracy cannot rank intrinsic model quality. Correct responses before the deadline
divided by **all offered requests** remains the primary paired task metric. Report
per-run differences and retain negative or inconsistent outcomes. This balanced
sample still covers only ten dog breeds, with unknown pretraining overlap; it is
not representative of all ImageNet classes or another device. Data retains the
same [Oxford attribution and license](https://www.robots.ox.ac.uk/~vgg/data/pets/).

## Fresh-image validation result — 2026-10-12

All twelve workers completed with the frozen validation manifest and OS network
denial; an outbound probe again returned `EPERM`. Hardware and versions match the
background follow-up above. Tests and asset preparation finished before measurement.
No other model benchmark ran concurrently; background OS activity was not controlled.
There are **140 unique images**, repeated three times per strategy, and 1,680 total
request attempts. These results are not pooled with either earlier experiment.

| Strategy | Served / offered | Correct | Correct by 100 ms | Abstained | Late | All-request p95 range | Peak process RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed MobileNetV2 | 360 / 420 | 303 | 303 | 60 | 0 | 32.9–34.7 ms | 121.0 MiB |
| Fixed ResNet18 | 180 / 420 | 156 | 154 | 240 | 5 | 53.7–54.7 ms | 206.1 MiB |
| Blocking adaptive | 360 / 420 | 303 | 300 | 60 | 6 | 54.1–54.5 ms | 251.4 MiB |
| Background adaptive | 360 / 420 | 306 | 306 | 60 | 0 | 53.6–55.0 ms | 271.1 MiB |

Per-run useful counts were fixed-small **101, 101, 101**; fixed-large **51, 52, 51**;
blocking adaptive **100, 100, 100**; background adaptive **102, 102, 102**.
The matched task advantage therefore persisted at two additional useful responses
per run over blocking adaptation and one over fixed-small. Background also changed
raw correctness by serving a different model on transition requests. This is a small
observed benefit with a higher process peak, not an intrinsic model-quality ranking.

An independent audit checked all 1,680 requests, unique phase assignments, identical
traces, deadlines, attribution, failures, summaries, source snapshots, and unchanged
assets. The paired differences are small and concrete in every repetition:

- Request 20, `beagle_35`: background's MobileNet answer was correct and on time;
  blocking adaptation's ResNet answer was incorrect and late.
- Request 80, `english_setter_84`: both answers were correct; only background
  replacement completed by the deadline.
- Against fixed-small, request 34, `great_pyrenees_52`, explains the entire one-result
  advantage: ResNet was correct while MobileNet predicted kuvasz.

Thus the background-versus-blocking gain combines one model-choice correctness
difference and one deadline-only improvement per run. Repeating these same images
does not create additional independent evidence about classification quality.

A counterexample also remains: during the failed-replacement phase, request 72
(`great_pyrenees_88`) was misclassified as chow by background's retained MobileNet,
while fixed-large's resident ResNet was correct. Continued serving does not guarantee
the best prediction, and retaining an already-resident model changes this tradeoff.

Every run recorded zero inference errors, unexpected activation errors, and drops.
Each adaptive run preserved serving through one expected failed replacement and
recovered after restoration. No deadline, image, label, policy, or ordering rule was
retuned after inspecting these predictions. Zero misses across three background
runs remains an observation, not a hard deadline guarantee or independent replication
on another device. Memory is still whole-process high-water RSS.

Raw runs, protocol/workload snapshots, summaries, isolation note, selection script,
and exact measurement sources are retained in `.demo-assets/live-swap/validation-20261012/`.
The frozen manifest SHA-256 is
`67e081ac13837d79d3cdf9ff8439cfac282bb5ecd5d4dcec5f1972adb2eaa027`.

## Session lifetime and swap churn

Protocol declared before measurement on 2026-10-12: three fresh processes for each
of `fixed-small`, `fixed-large`, and `swapping`, run serially, rotating that order
left by repetition index. Every process performs 31 reconciliations and 31 real
Chelsea inferences. Swapping starts with MobileNet and alternates models thirty
times, constructing 31 sessions; each fixed strategy constructs only one session.
The models, preparation, native CPU thread counts, and input are unchanged.

```bash
python examples/live_swap/churn.py --strategy fixed-small --records /tmp/churn-small-1.jsonl
python examples/live_swap/churn.py --strategy fixed-large --records /tmp/churn-large-1.jsonl
python examples/live_swap/churn.py --strategy swapping --records /tmp/churn-swapping-1.jsonl
```

Each command starts a fresh process and requires a fresh record path. Repeat three
times with rotated order and new suffixes. No files download during measurement.
Every constructed session warms once; fixed runs therefore warm once and swapping
runs warm 31 times. This experiment measures lifetime and process residency, not
task quality, latency, or energy. There is no deadline or simulated battery trace.

The factory stores only weak references to sessions. After every inference the
runner awaits a barrier on its **single executor worker**, ensuring earlier worker
callables have been released before sampling. This serial protocol intentionally
differs from the two-worker concurrent comparison above and is not pooled with it.
The current output stays alive through the next reconciliation, is checked for
unchanged bytes, and is released before the next inference. Samples therefore keep
one current output, never an accumulating output history. A normal return drops
TEMMS/runtime/results and joins the executor before final lifetime observations.
There is no per-step forced collection; one final `gc.collect()` is separately labeled.

Acceptance: exact requested/active/serving identities, finite 1,000-class outputs,
31 versus one constructed session as specified, only the active Python wrapper at
each quiescent boundary, and no wrappers after owner/executor disposal. Concurrency,
cancellation, and exception ownership are covered separately by bounded tests in
[test_onnx_session_lifetime.py](../../tests/test_onnx_session_lifetime.py). A failed
worker or caller-retained exception traceback may legitimately keep a session alive;
clearing diagnostics inside TEMMS would conceal the actual owner.

Raw JSONL records include source and asset hashes, versions, timestamps, output
hashes, created/live instance indices, current RSS, and process high-water RSS.
Report first/last and min/max RSS at **matching model identities**, plus final
post-disposal observations and each process separately. No memory-growth threshold
is selected after seeing results. RSS includes imports, preprocessing, both native
allocators, instrumentation, and any temporary old/new overlap; it is not model-only
memory or TEMMS overhead. Current samples miss brief peaks; high-water cannot fall.

On macOS, the probe reads its own task's resident bytes using Apple's
[MACH_TASK_BASIC_INFO structure](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/mach/task_info.h).
On Linux it reads `Rss` from `/proc/self/smaps_rollup`, converting KiB to bytes.
Unavailable measurements are null with an error. This environment prevents launching
`ps`, so the macOS measurement uses the in-process API and adds no dependency.
Weak references establish Python wrapper lifetime, not native allocator release.
See [Python weak references](https://docs.python.org/3.11/library/weakref.html) and
[ONNX Runtime arena allocation](https://onnxruntime.ai/docs/get-started/with-c.html).
Bounded successful runs cannot establish unbounded leak freedom or hard memory limits.

### Swap-churn result — 2026-10-12

All nine fresh workers completed under OS network denial. The machine and package
versions match the validation experiment above. There were **279 attributed
inferences**, 90 replacements, and 99 constructed sessions across all runs. Every
sample contained only the active Python session wrapper; none remained after owners
and workers were dropped, before or after forced collection. Retained output bytes
survived the next reconciliation unchanged. Both models predicted Egyptian cat for
Chelsea throughout. No runtime errors or unavailable RSS samples occurred.

Process memory nevertheless increased substantially during swapping and remained
resident after wrapper collection. Below, first/last values compare the same model:
MobileNet at steps 0 and 30 for swapping, the fixed model otherwise. All values are
MiB (1,048,576 bytes), and each row is a separate process.

| Strategy | Repetition | First matching RSS | Last matching RSS | Peak RSS | After owner drop and GC |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fixed MobileNet | 1 | 113.42 | 113.44 | 113.50 | 113.36 |
| Fixed MobileNet | 2 | 117.34 | 117.34 | 117.41 | 117.27 |
| Fixed MobileNet | 3 | 113.64 | 113.64 | 113.69 | 113.55 |
| Fixed ResNet | 1 | 196.03 | 196.11 | 196.16 | 196.02 |
| Fixed ResNet | 2 | 196.83 | 196.83 | 196.88 | 196.73 |
| Fixed ResNet | 3 | 201.03 | 201.05 | 201.09 | 200.95 |
| Swapping | 1 | 112.23 | 269.48 | 269.52 | 269.38 |
| Swapping | 2 | 112.05 | 268.61 | 268.64 | 268.50 |
| Swapping | 3 | 121.12 | 273.38 | 273.41 | 273.27 |

For each row, the observed same-model minimum/maximum equal the first/last endpoints
above. The final five MobileNet observations in swapping runs were respectively
`259.80, 259.80, 266.20, 269.48, 269.48`; `268.61` five times; and
`273.34, 273.34, 273.38, 273.38, 273.38` MiB. The first run still grew within that tail;
these short runs do not establish a stable plateau. The final GC changed no observed
RSS value. No threshold was retrofitted to turn this result into a memory-efficiency win.
Each swapping run lasted only about 2.4–2.5 seconds after preparation. An independent
audit verified all 315 records, identities, output fingerprints, memory samples,
source snapshots, and asset hashes; this is still a short serial experiment.

This establishes wrapper retirement for the measured paths. It does **not** locate
the retained native/process memory or distinguish allocator caching from a native
leak. A matched direct-ONNX load/drop control is needed before attributing that
retention to TEMMS. No lifecycle API, allocator tuning, or core change was justified
by the result. Background OS activity remained uncontrolled.

Raw records, per-process summaries, frozen protocol/order, isolation note, and exact
premeasurement source archive are retained in `.demo-assets/live-swap/churn-20261012/`.
Frozen runner SHA-256:
`4149f64f43c2209a03cbc6ffe3f8cd4cdc6393f16dbe8db3fb47e57ea06d9d15`.

### Matched direct-ONNX control — declared 2026-10-12

The earlier RSS increase does not show where allocations remain. The next experiment
compares **TEMMS plus its ONNX adapter** against direct ONNX construction/inference.
The runner now accepts `--backend temms|direct`; the default remains `temms`.
Both use the same 31-step fixed-small/fixed-large/swapping schedules, models, input,
warmup, output lifetime, executor and native thread settings, and sampling boundaries.
Both import the same support modules before their prepared memory baseline.

```bash
python examples/live_swap/churn.py --backend temms --strategy swapping --records /tmp/managed-churn-1.jsonl
python examples/live_swap/churn.py --backend direct --strategy swapping --records /tmp/direct-churn-1.jsonl
```

The direct path constructs and warms each replacement on the same single worker.
Only after warmup returns does the caller assign it as active and release the old
session. It keeps no old-session alias. Fixed strategies reuse one session. Direct
inference copies the feed mapping and returns the same output tuple representation;
both backends retain only that tuple across the next reconciliation. Direct model
identity is attributed by the driver from its verified artifact/configuration;
the TEMMS path takes attribution from the runtime result. Matched output hashes
provide a separate consistency check. The control does not instantiate TEMMS or
OnnxRuntime. It tests serial execution, not failure-recovery parity or concurrency.

Freeze eighteen fresh processes: three repetitions of all three strategies with
both backends. Rotate strategy order left by the zero-based repetition. Within
each adjacent backend pair, reverse `temms,direct` when repetition plus the original
strategy index is odd. Each strategy therefore experiences both backend orders,
but only a 2:1 split across three repetitions: partial counterbalancing. Headers
record backend and PID; retain the exact launch order before measurement.

Require the same active/serving sequence and per-model output hashes in each pair,
31 finite inferences, expected construction counts, only the active wrapper at each
boundary, and none after owner/worker disposal. Record all process RSS observations
without an after-the-fact threshold. Report matching-model first/last/min/max, final
tail samples, post-disposal values, and paired differences. Re-run both paths fresh;
do not pool the earlier nine-process experiment into this comparison.

Similar growth would establish that TEMMS/adapter ownership is unnecessary for the
observed pattern; it would not identify a native allocator or exclude a native leak.
Different results would require examining timing, Python allocations and ownership
before assigning a cause. ORT's pinned CPU arena destructor frees regions through
its underlying allocator; memory still resident after disposal must not be explained
as dead arenas intentionally keeping memory forever. See the
[v1.24.1 arena destructor](https://github.com/microsoft/onnxruntime/blob/v1.24.1/onnxruntime/core/framework/bfc_arena.cc#L71-L83)
and [CPU allocator](https://github.com/microsoft/onnxruntime/blob/v1.24.1/onnxruntime/core/framework/allocator.cc#L133-L163).

### Matched direct-ONNX result — 2026-10-12

All eighteen fresh workers completed under OS network denial, on the same M1 Pro
and package versions as the earlier primary runs. Tests and source/protocol freezing
preceded measurement; no other model run was scheduled concurrently, though OS
background activity was uncontrolled. There were **558 inferences, 180 replacements,
and 198 constructed sessions**. Only the active wrapper survived each sampling
boundary, and none survived owner/executor disposal. Both paths preserved retained
outputs and produced identical per-model output hashes across all paired requests.

Each row below is a fresh process. RSS values are MiB. Fixed rows compare their
resident model at steps 0/30; swapping rows compare MobileNet at steps 0/30.

| Strategy | Backend | Repetition | First matching RSS | Last matching RSS | Peak RSS | Post-disposal/GC RSS |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Fixed MobileNet | TEMMS | 1 | 112.38 | 112.39 | 112.44 | 112.30 |
| Fixed MobileNet | Direct | 1 | 113.69 | 113.70 | 113.75 | 113.61 |
| Fixed MobileNet | TEMMS | 2 | 112.23 | 112.27 | 112.31 | 112.17 |
| Fixed MobileNet | Direct | 2 | 113.89 | 113.89 | 113.94 | 113.80 |
| Fixed MobileNet | TEMMS | 3 | 114.52 | 114.53 | 114.58 | 114.44 |
| Fixed MobileNet | Direct | 3 | 117.38 | 117.38 | 117.42 | 117.28 |
| Fixed ResNet | TEMMS | 1 | 196.66 | 196.67 | 196.72 | 196.58 |
| Fixed ResNet | Direct | 1 | 196.45 | 196.47 | 196.52 | 196.38 |
| Fixed ResNet | TEMMS | 2 | 196.17 | 196.23 | 196.28 | 196.14 |
| Fixed ResNet | Direct | 2 | 196.95 | 196.95 | 197.00 | 196.86 |
| Fixed ResNet | TEMMS | 3 | 194.58 | 194.58 | 194.62 | 194.48 |
| Fixed ResNet | Direct | 3 | 203.80 | 172.23 | 203.81 | 172.14 |
| Swapping | TEMMS | 1 | 113.78 | 254.44 | 261.02 | 254.33 |
| Swapping | Direct | 1 | 112.14 | 273.94 | 273.97 | 273.83 |
| Swapping | TEMMS | 2 | 111.56 | 273.45 | 273.48 | 273.34 |
| Swapping | Direct | 2 | 111.94 | 243.28 | 246.56 | 243.17 |
| Swapping | TEMMS | 3 | 111.95 | 241.17 | 274.14 | 241.06 |
| Swapping | Direct | 3 | 112.53 | 271.02 | 272.67 | 270.91 |

For swapping, TEMMS-minus-direct differences in first-to-last RSS growth were
**−21.14, +30.55, −29.27 MiB**. They change sign: these runs do not establish a
consistent incremental memory cost for TEMMS plus its adapter. Direct growth of
**131.34–161.80 MiB** reproduces the pattern without either component; TEMMS/adapter
ownership is therefore not necessary for this observed growth.

Current RSS is not monotonic. TEMMS swapping repetition 3 dropped about 30 MiB on its
last step, and direct fixed-ResNet repetition 3 fell 31.56 MiB despite keeping the same
session. Do not discard these observations or treat final RSS as a peak. Full raw
traces retain each model's range and tail. Final forced GC reduced no observed RSS
value. This comparison does not distinguish allocator reuse, fragmentation, page
residency changes, native references, or leakage. No native-memory bound or portable
ranking follows from thirty swaps per process.

No core, adapter, lifecycle, or default allocator change was made. Further memory
work should test the documented CPU-arena option over a predeclared longer horizon,
including latency and unchanged-output checks, before offering an opt-in recipe.
Raw records, frozen order, sources and audits are retained under
`.demo-assets/live-swap/direct-control-20261012/`. Frozen runner SHA-256:
`7f39da0de7917abd43155fd1a6c7da71edb7cd4dda8380352a7ce12022f65ded`.

### CPU-arena tradeoff — declared 2026-10-12

The direct control reproduced RSS growth without TEMMS. This experiment changes one
documented native setting, `SessionOptions.enable_cpu_mem_arena`, through the existing
injected session factory. The option controls the CPU allocation arena; memory-pattern
optimization and memory reuse are separate settings and remain at their ONNX defaults.
The factory checks the constructed session's arena setting. The runner records the
requested setting and the unchanged memory-pattern/reuse defaults. See the
[ONNX Python option](https://onnxruntime.ai/docs/api/python/api_summary.html#onnxruntime.SessionOptions.enable_cpu_mem_arena)
and [pinned option definitions](https://github.com/microsoft/onnxruntime/blob/v1.24.1/onnxruntime/core/framework/session_options.h#L101-L115).

The CLI adds `--cpu-arena on|off` and positive `--steps`. Defaults remain arena on
and 31 steps, and the other demo/comparison commands retain their existing settings.
For this experiment freeze **301 steps / 300 replacements**, swapping only, both
TEMMS and direct paths, arena on/off, and three fresh repetitions per cell: twelve
serial processes. This is ten times the previous replacement count, not an unbounded
soak test. Fixed strategies are unnecessary for this narrow question; no recommendation
about long-lived steady-serving sessions can follow from it.

```bash
python examples/live_swap/churn.py --strategy swapping --backend temms --cpu-arena on --steps 301 --records /tmp/arena-on-1.jsonl
python examples/live_swap/churn.py --strategy swapping --backend temms --cpu-arena off --steps 301 --records /tmp/arena-off-1.jsonl
```

Predeclared process order, with `T` meaning TEMMS plus its adapter, `D` direct ONNX,
`+` arena on and `−` off:

| Repetition | Exact order |
| --- | --- |
| 1 | T+, T−, D−, D+ |
| 2 | D+, D−, T−, T+ |
| 3 | T−, T+, D+, D− |

Each cell occupies three different positions and each backend experiences both arena
orders. Three repetitions provide partial counterbalancing, not full order balance.
Keep the same pinned models/photo, preprocessing, native threads, one-worker executor,
old/new overlap, output lifetime, weakrefs, barriers and GC treatment in all cells.
No per-step collection or allocator release request is added.

The shared factory now performs the single warmup and records scalar construction
and warmup durations. Runtime/direct warmup calls were removed to prevent duplication.
This changes instrumentation equally in both paths; earlier timing/RSS results remain
historical and are not pooled. Each session still warms before publication.

Timing endpoints are fixed before measurement:

- `load_ms`: factory call through native session construction, configuration and metadata checks.
- `warmup_ms`: the one warmup inference, including its feed copy and output disposal.
- `reconcile_ms`: caller wall time through replacement publication and old-session release,
  including worker dispatch and, for TEMMS, policy/orchestration. This is not pure load time.
- `infer_ms`: invocation through finite-output validation, argmax and label interpretation.
- `step_ms`: reconciliation start through that same interpretation endpoint. It includes
  verification/release of the previous output between replacement and inference.

New-output hashing, the worker barrier, RSS sampling, JSONL writes and their associated
delays are outside `infer_ms`/`step_ms`. Preprocessing occurs once before the trace.
Monotonic sample timestamps retain elapsed time including observation overhead.
There are no scheduled arrivals or queue, so these service durations are **not**
100 ms deadline outcomes from the earlier application comparison.

Report step 0 separately from the 300 replacements, and replacement timings by
model/process using sample count, nearest-rank p50/p95 and maximum. Retain every
RSS observation, per-model first/last/min/max, last fifty matching-model samples,
process peak and post-disposal values. The 300 steps are correlated observations,
not 300 independent experimental repetitions.

Acceptance requires twelve complete 301-step runs, 301 constructions/warmups per
run, correct identities, finite predictions, unchanged retained outputs, per-model
output hashes matching across all four cells, valid RSS, only the active wrapper at
each boundary, and no survivors after owner/worker disposal. A positive memory claim
requires arena-off to reduce **both peak and final matching-model RSS in all three
paired TEMMS repetitions**; otherwise report mixed evidence. Direct pairs provide
corroboration. Publish unfavorable latency changes and initial-load costs regardless.
Without a deployment budget, a slowdown cannot be declared acceptable or the option
recommended universally. Defaults stay unchanged; a reproducible opt-in recipe is
considered only if the measured tradeoff supports one.

### CPU-arena result — 2026-10-12

**The predeclared memory improvement criterion was not met.** Arena off lowered
TEMMS peak RSS in all three repetitions, but final matching-model RSS increased
in repetition 3. Keep defaults unchanged and do not promote arena off as a general
memory remedy. The configurable probe remains useful for reproducing this mixed
result; no application recipe or new runtime lifecycle API is added.

All twelve fresh serial workers completed: **3,612 inference steps and constructed
sessions, 3,600 replacements, 3,660 records, 3,648 memory observations**. Each step
had only its active Python session wrapper; none survived owner/worker disposal,
before or after forced GC. Model identities and output hashes matched across all
cells. Frozen code checks finite scores and retained-output integrity during the
run; full score arrays are not retained for the independent audit to recompute.
Every stderr was empty, and the OS network-denial probe returned `EPERM`.

Environment: Apple M1 Pro, 10 logical CPUs, macOS 26.6.2 arm64, Python 3.11.8,
ORT 1.24.1, NumPy 2.4.2, Pillow 12.1.0, TEMMS 0.2.0a1; CPU provider, native threads
1/1 and one worker. Sources, tests and protocol were frozen before measurement.
No other model benchmark ran concurrently; background OS activity was uncontrolled.

Whole-process memory in MiB; final means MobileNet at step 300 in both settings:

| Backend | Repetition | Peak on | Peak off | Final on | Final off |
| --- | ---: | ---: | ---: | ---: | ---: |
| TEMMS | 1 | 274.11 | 258.83 | 246.47 | 235.81 |
| TEMMS | 2 | 275.92 | 257.16 | 251.48 | 236.73 |
| TEMMS | 3 | 276.02 | 258.42 | 254.08 | 258.38 |
| Direct | 1 | 275.98 | 268.22 | 268.48 | 268.19 |
| Direct | 2 | 275.97 | 252.23 | 273.75 | 236.41 |
| Direct | 3 | 276.97 | 257.28 | 276.94 | 257.25 |

TEMMS off-minus-on peak differences were **−15.28, −18.77, −17.59 MiB**; final
matching-model differences were **−10.66, −14.75, +4.30 MiB**. Direct pairs favored
off at both endpoints but cannot replace the failed managed-path criterion.
Per-model first/last/min/max and all final fifty matching-model observations are
retained in the audit. Current RSS still falls and rises within traces; no plateau
or hard bound is established. Six post-GC samples increased by 10.72–12.63 MiB,
while the other six were unchanged. None decreased. Wrapper collection and process
residency remain distinct; this experiment does not identify a native allocator cause.

Latency effects also change direction. Each replacement distribution contains
**150 observations per model per process**, with initial step 0 excluded. These
are correlated steps from three process repetitions, not 450 independent replicates.
Selected full-step p95 values in milliseconds, with every other metric preserved in
the audit rather than selecting only favorable runs:

| Backend | Repetition | MobileNet on → off | ResNet on → off |
| --- | ---: | ---: | ---: |
| TEMMS | 1 | 47.26 → 54.89 | 129.49 → 142.37 |
| TEMMS | 2 | 45.47 → 43.58 | 125.17 → 119.74 |
| TEMMS | 3 | 44.11 → 43.58 | 119.27 → 119.36 |
| Direct | 1 | 42.21 → 42.80 | 114.50 → 117.77 |
| Direct | 2 | 41.22 → 43.26 | 112.27 → 115.78 |
| Direct | 3 | 42.60 → 41.89 | 118.62 → 114.56 |

Adverse tails are visible: TEMMS repetition 3 ResNet maximum full-step time rose
from 137.85 to 183.80 ms; direct repetition 2 rose from 119.52 to 233.72 ms.
The separate initial MobileNet step ranged **45.06–65.39 ms** overall; construction
13.13–32.23 ms, warmup 15.44–16.71 ms, reconciliation 29.12–49.20 ms and interpreted
inference 15.42–16.92 ms. These ranges summarize different processes, not component
values to add together. The audit reports each initial observation and every
replacement load/warmup/reconcile/infer/step n/p50/p95/max separately.

There were no arrivals or deadlines in this probe: a 100+ ms service duration is not
a deadline miss. It also measures inference only after a new session and one warmup,
so it says nothing about steady serving. No deployment slowdown budget, portable
memory saving, energy saving, or native leak-freedom claim follows.

All raw runs, the 47-file source archive, exact order, isolation probe and independent
`independent-audit.py`, `.json`, and `.txt` are retained in
`.demo-assets/live-swap/arena-20261012/`. Frozen runner SHA-256:
`999c11fa94f2ca7ba95ee47e6570e174c73c2ec4cdb45b35c0c1e9e5d41b473a`.
Earlier experiments retain their original source/metadata and are not pooled.
