# Qualify TEMMS under an edge CPU budget

This headless qualification runs real MobileNetV2 and ResNet18 classifiers through
TEMMS under a Linux-enforced CPU and memory budget. It exercises startup, model
replacement, CPU contention, a corrupt replacement, recovery, repeated swaps and
a fresh-container restart. The inputs are the existing twenty pinned Oxford-IIIT
Pet photographs; every served output is checked against direct ONNX execution for
that exact image and model.

The target is **native Ubuntu 24.04 on x86_64 and ARM64**, using Docker and Python
3.12 inside the image. Both architectures passed the
[first hosted qualification](https://github.com/LewisJor/temms/actions/runs/39485701787)
at `827b7242`. The
[verification record](../../docs/progress.md) identifies completed runs and exact
revisions; a workflow configuration alone is not a passing result. Docker is a
qualification tool outside the library. TEMMS core still has zero required runtime
dependencies.

## Prepare and run

Use a native Linux host with Docker Engine and cgroup v2. The qualification reads
back the limits, so a host that cannot enforce them does not qualify. Docker Desktop
or an emulated ARM container is not a substitute for the stated native target.
Start from a [TEMMS checkout](../../README.md#install-and-try), with a Python 3.12
environment and access to Docker. Run as a non-root account from the repository
root; the launcher maps that account's UID and GID into each container:

```bash
python3.12 -m venv .venv-edge
source .venv-edge/bin/activate
python -m pip install '.[dev,onnx,demo]'
python examples/live_swap/assets.py
python examples/live_swap/workload.py
python -m build --wheel --outdir dist
docker build -f examples/edge/Dockerfile -t temms-edge .
```

Asset preparation downloads about 61 MB of shared model assets and 655 KB of images.
It verifies pinned sizes and hashes and reuses valid files. The image build also
needs networking to obtain its base image and dependencies. Both steps happen
before the offline qualification.

The [Dockerfile](Dockerfile) pins the Ubuntu base image by digest and installs
ONNX Runtime 1.24.1, NumPy 2.4.2 and Pillow 12.1.0 with the built TEMMS wheel.
Ubuntu's actual Python 3.12 patch is recorded in each worker's header;
pinning the base image does not freeze packages fetched from its repositories.

Choose a fresh output directory, then run:

```bash
mkdir -p "$PWD/.demo-assets/edge"
bash examples/edge/qualify.sh temms-edge \
  "$PWD/.demo-assets/live-swap" \
  "$PWD/.demo-assets/edge/first-run"
```

The arguments are `IMAGE ASSETS OUTPUT`. The launcher runs two fresh worker containers
sequentially, then a separate verifier container, preserving evidence from each.
Keep the entire output directory when reporting a result. Reusing an output
directory would mix separate experiments;
choose another name for the next run.

## What the container enforces

| Boundary | Required setting and evidence |
| --- | --- |
| CPU | One CPU's worth of scheduling quota for the entire container; cgroup readback and throttling counters. |
| Memory | 512 MiB for the whole container; memory peak and OOM-event counters retained. |
| Swap | Disabled; `memory.swap.max` must be zero. |
| Network | Docker `--network none`; the worker verifies external connectivity is unavailable. |
| Filesystem | Read-only root filesystem and original asset mount; only designated temporary and evidence locations are writable. |
| User | The worker runs as a non-root user. |

The CPU limit is a quota, not a reserved or exclusive physical core. The memory
budget includes the Python process, native sessions and contention helpers.
Docker disables swap by setting its combined memory-and-swap limit equal to the
memory limit; `--memory-swap=0` would leave that setting unset. See Docker's
[resource-limit semantics](https://docs.docker.com/engine/containers/resource_constraints/).
The [none network driver](https://docs.docker.com/engine/network/drivers/none/)
retains loopback but provides no external network interface.

These limits are applied by Linux and Docker. TEMMS selects from application-supplied
context and delegates activation and inference to its injected runtime; it does
not create containers, measure battery state or enforce the limits itself.

## The scenario

The application supplies `input_ready` and `detail_required` context. The scenario
changes those requests explicitly; it does not automatically detect CPU pressure
and select a model. Each worker performs the same 113 inference requests:

| Phase | Requests | Required behavior |
| --- | ---: | --- |
| Empty runtime and startup | 20 | Begin with no active model, activate MobileNetV2 and serve every input. |
| Larger model | 20 | Activate ResNet18 and serve every input. |
| CPU contention | 20 | Run two CPU-consuming peer processes inside the same quota, switch to MobileNetV2 and continue serving. |
| Corrupt replacement | 20 | Corrupt a private ResNet18 copy, observe a native ONNX parsing failure, and keep serving with MobileNetV2. |
| Recovery | 20 | Restore the private artifact, activate ResNet18 and serve every input. |
| Repeated replacement | 12 | Alternate between the models twelve times, checking one inference after each swap. |
| Missing context and resume | 1 | Omit required input health, abstain without clearing the active session, then restore context and serve with MobileNetV2. |

Forty direct references are prepared before testing the empty runtime. This warms
native code and the filesystem cache; the startup phase does not measure a cold
device or cold model load.

The original pinned models remain read-only. The corrupt-artifact fixture exercises
a recoverable session-construction failure. It does not simulate an OOM kill or
claim that the process survives one.

After the first worker exits, the launcher creates a second container from the same
image and repeats the whole scenario. That worker must also start with no active
model: no active-session state is persisted or restored between containers. This
is a fresh-process restart check, not seamless service during a crash.

## Read the evidence

The output includes `run-1/events.jsonl` and `run-1/summary.json`, with matching
files under `run-2/`. The `run-1-container.json` and `run-2-container.json` files
retain Docker's observed configuration and exit state; `run-1.log` and `run-2.log`
retain worker output. Image and Docker-host metadata are saved alongside them.
The launcher prints the verifier's result and saves it in `verification.log`, with
`verification-container.json` recording that separate container's exit and limits.
Use the exact source revision that produced the records when auditing them.
Each run also retains the pinned `labels.txt`, allowing the verifier to check
displayed labels as well as numerical predictions without downloading anything.

Acceptance requires all of the following:

- Both fresh workers finish the declared scenario and start without an active model.
- Every successful request is attributed to its model at admission, and its full
  1,000-score vector matches the reference for that image and model. All twenty
  images and both models receive checked service; there are forty direct reference
  vectors, checked with `rtol=1e-5` and `atol=1e-6`. A matching top prediction alone
  is insufficient.
- The corrupt replacement fails before publication, the prior model continues
  serving, and the restored artifact can activate later.
- Missing required context produces no selected model; the application abstains
  and later resumes service when valid context returns.
- The contention helpers consume CPU time, and cgroup counters show throttling
  during contention. Merely launching helper processes is insufficient evidence.
- The configured CPU, memory, swap, network, user and filesystem restrictions match
  the observed environment. OOM events or unsuccessful container exits fail the run.

Expected labels, predictions, full references and returned scores remain in the
records. A classifier can predict the wrong breed while still passing the routing
check; classification accuracy is reported separately from execution integrity.
The direct references share preprocessing and ONNX Runtime with the serving path,
so they do not independently establish the engine's numerical correctness.

Activation and inference timings are descriptive. Compare them only with the
recorded architecture, image, dependencies, quota and contention conditions. This
scenario sets no deadline and establishes no general latency or throughput promise.
Whole-container memory accounting is also distinct from process RSS.
This constrained trace awaits each replacement before serving the next request.
The separate [native in-flight test](../../tests/test_onnx_runtime_integration.py)
checks inference overlapping publication; its installed-wheel execution is required
in both architecture jobs.

## GitHub Actions

The [**Constrained native Linux qualification** workflow](../../.github/workflows/edge.yml)
runs the same procedure on native `ubuntu-24.04` x86_64 and `ubuntu-24.04-arm`
ARM64 runners. It does not use QEMU. Each architecture runs two fresh workers,
giving 226 checked requests per architecture and 452 across the matrix.
The host test interpreter is pinned separately from Ubuntu's Python 3.12 package
inside the image; the image's exact Python patch and dependencies are recorded.
Before the containers run, CI checks the installed wheel against the source and
requires the full test suite with zero skips, including all three native ONNX
integration cases.

Open **Actions → Constrained native Linux qualification**, select a completed run
for the commit you want to inspect, and download `edge-x64-<run>-<attempt>` and
`edge-arm64-<run>-<attempt>`. Each artifact contains the container evidence under
`qualification/`, plus the installed-package test results, source/wheel provenance
and build logs. Artifacts are retained for 30 days. Check each job's conclusion
alongside its records. Failure artifacts are useful for diagnosis but do not
establish qualification. Refer to [progress](../../docs/progress.md) for the actual
hosted status rather than assuming this configured matrix has passed.

## Limits and troubleshooting

- If preparation reports a missing or corrupt asset, rerun the two preparation
  commands while connected. The qualification does not download missing weights.
- If Docker or cgroup checks fail, inspect the host and saved container configuration.
  Do not remove the constraints to turn a failed qualification into a pass.
- If an OOM event, timeout or native error occurs outside the deliberate corrupt
  replacement, preserve the logs and limits. The workload must fit the declared
  budget; there is no hard-OOM fallback mechanism.
- If an output or attribution check fails, retain the full records. Relaxing score
  tolerance or skipping inputs changes the acceptance criteria.

This is a constrained native CPU integration test, not physical Raspberry Pi or
Jetson qualification. It does not measure battery life, thermal throttling, device
power, intermittent model downloads or sensor behavior. Local inference uses
prepared files, so disconnected networking tests deployment independence rather
than the effect of packet loss on inference.

The [terminal replay](../live_swap/README.md) remains useful for inspecting decisions
interactively. The earlier [Webots photo-exhibit tutorial](../webots/README.md) is an
optional visual example. Model and image terms remain in the existing
[provenance guide](../live_swap/README.md#model-and-image-provenance),
[asset manifest](../live_swap/assets.json) and [workload manifest](../live_swap/workload.json).
