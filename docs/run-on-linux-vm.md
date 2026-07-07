# Run TEMMS on a Linux VM

This guide is the minimum repeatable path for running one TEMMS edge agent on one Linux VM. Use the same flow for x86_64 VMs, ARM64 VMs, Jetson-class systems, and Raspberry Pi-class systems; device-specific runtime packages can be layered after the base agent works.

## 1. Prepare the VM

Requirements:

- Linux with systemd
- Python 3.11+
- 4 GB RAM recommended
- Network access for install, unless you pre-stage wheels and model packages

Create a dedicated user and directories:

```bash
sudo useradd -r -s /bin/false temms || true
sudo mkdir -p /opt/temms /var/lib/temms/{models,cache,packages} /etc/temms/policies /var/log/temms
sudo chown -R temms:temms /var/lib/temms /var/log/temms
sudo chown temms:temms /etc/temms/policies
sudo chmod 0750 /etc/temms/policies
```

## 2. Install TEMMS

Use the installer from a packaged checkout or release bundle:

```bash
sudo TEMMS_EXTRAS=inference deploy/install.sh
```

This creates the `temms` system user, `/opt/temms` virtualenv, `/etc/temms` config, a `temms`-writable `/etc/temms/policies` active policy directory, `/var/lib/temms` state directories, `/var/log/temms`, and the `temms.service` systemd unit. On first install it also appends a generated `TEMMS_API_TOKEN` to `/etc/temms/temms.env`, so `/v1/control/*`, `/v1/hub/*`, and Web UI write endpoints are protected when the service starts.

For local non-root rehearsals without the installer, the daemon keeps the
systemd defaults but falls back to `$XDG_STATE_HOME/temms` or
`~/.local/state/temms` when `/var/lib/temms` or `/etc/temms/policies` cannot be
created by the current user.

For fleet images that use different mount points, the installer honors `PREFIX`,
`CONFIG_DIR`, `DATA_DIR`, `LOG_DIR`, and `SERVICE_NAME` and renders the initial
systemd unit, `temms.yaml`, and `temms.env` with the same paths:

```bash
sudo PREFIX=/srv/temms \
  CONFIG_DIR=/etc/temms \
  DATA_DIR=/srv/temms-state \
  LOG_DIR=/srv/temms-logs \
  SERVICE_NAME=temms-edge \
  TEMMS_EXTRAS=inference \
  deploy/install.sh
```

Set `/etc/temms/temms.env` per VM:

```ini
TEMMS_HOST=0.0.0.0
TEMMS_PORT=8080
TEMMS_DATA_DIR=/var/lib/temms
TEMMS_API_TOKEN=<generated-by-installer>
# TEMMS_HUB_URL=http://hub-vm:8080
# TEMMS_HUB_TOKEN=change-me
# TEMMS_DEVICE_ID=edge-1
# TEMMS_DEVICE_PROFILE=x86_64-cpu
# TEMMS_HUB_SYNC_INTERVAL_S=30
# TEMMS_HUB_AUTO_APPLY=false
# TEMMS_ROLLOUT_REQUIRE_SIGNATURE=true
# TEMMS_PACKAGE_SIGNING_KEY_FILE=/etc/temms/hub-signing.key
```

Manual installation is also possible.

From a checkout or release bundle:

```bash
sudo python3 -m venv /opt/temms/venv
sudo /opt/temms/venv/bin/pip install --upgrade pip
sudo /opt/temms/venv/bin/pip install ".[inference]"
```

Install `zstd` or the Python `zstandard` module if this VM needs to import `.temms.tar.zst` archives.

For development or simulation VMs, use:

```bash
sudo /opt/temms/venv/bin/pip install ".[dev,sim]"
```

## 3. Initialize the edge agent

```bash
sudo /opt/temms/venv/bin/temms init \
  --config /etc/temms/temms.yaml \
  --data-dir /var/lib/temms
```

Run diagnostics before importing models:

```bash
sudo /opt/temms/venv/bin/temms doctor --config /etc/temms/temms.yaml
```

`temms doctor` reports OS, architecture, Python, optional runtimes, accelerators, writable paths, API port status, security readiness, and local model cache health. Security readiness confirms whether the control API token, Hub token source, rollout signature enforcement, and package signing key source are configured without printing secrets. Writable path checks use an actual create/write/delete probe in the configured directory or nearest existing parent, so they catch read-only mounts and ownership problems that permission bits alone can miss. Cache health verifies that every cached model file still exists, matches its recorded size, and matches its recorded SHA256. For fleet collection, add `--json` and save the result with your deployment evidence:

```bash
sudo /opt/temms/venv/bin/temms doctor \
  --config /etc/temms/temms.yaml \
  --json > temms-doctor.json
```

## 4. Import a model package

Copy a TEMMS package onto the VM, then import it. Edge package imports require
signature verification and strict production metadata by default; use
`--allow-unsigned-package --allow-lab-metadata` only for isolated labs:

```bash
sudo /opt/temms/venv/bin/temms import /path/to/package.temms.tar.zst \
  --config /etc/temms/temms.yaml \
  --require-signature \
  --signing-key-file /path/to/hub-signing.key \
  --device-profile x86_64-cpu
```

Imports are idempotent. Re-importing the same package refreshes model, package, and policy records instead of failing on duplicate IDs. Policies included in the package are copied into the active policy directory configured in `/etc/temms/temms.yaml`, and active policy files from earlier imports of the same package are removed when they are no longer declared by the package manifest.

Before importing into a target VM, validate the package against that VM's runtime stack:

```bash
sudo /opt/temms/venv/bin/temms package validate /path/to/package.temms.tar.zst \
  --signing-key-file /path/to/hub-signing.key \
  --device-profile x86_64-cpu \
  --check-runtime
```

## 5. Configure a slot

```bash
sudo /opt/temms/venv/bin/temms slot create vision \
  --description "Primary vision model" \
  --required \
  --default model-yolov8-daylight-001 \
  --config /etc/temms/temms.yaml
```

## 6. Enable the service

If you used `deploy/install.sh`, the unit is already installed. Otherwise install the systemd unit from `deploy/temms.service`, then start TEMMS:

```bash
sudo cp deploy/temms.service /etc/systemd/system/temms.service
sudo cp deploy/temms.env /etc/temms/temms.env
sudo systemctl daemon-reload
sudo systemctl enable temms
sudo systemctl start temms
```

Check health:

```bash
curl -fsS http://127.0.0.1:8080/v1/health
curl -fsS http://127.0.0.1:8080/v1/status
```

## 7. Benchmark on this VM

After import, benchmark a cached model against the actual VM runtime. If you
publish benchmark evidence to a remote Hub Lite API, set `TEMMS_HUB_TOKEN` to
that Hub VM's API token or pass `--token` explicitly:

```bash
sudo mkdir -p /var/lib/temms/benchmarks
sudo /opt/temms/venv/bin/temms benchmark model-yolov8-daylight-001 \
  --config /etc/temms/temms.yaml \
  --samples 20 \
  --warmup 3 \
  --output /var/lib/temms/benchmarks/model-yolov8-daylight-001.json \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN" \
  --device-id edge-1 \
  --package-id pkg-yolov8-daylight \
  --runtime-target-id temms-x86_64-cpu
```

The benchmark artifact records latency percentiles, derived throughput, selected runtime options, and detected VM capabilities so it can be included in evidence exports or compared across VMs. When `--hub-url` is provided, Hub Lite stores the benchmark as fleet evidence; list it later with `temms hub benchmarks --device-id edge-1`.

To intentionally run without a generated API token in an isolated lab, install with `sudo TEMMS_GENERATE_API_TOKEN=false deploy/install.sh`.

## 8. Control API token

Read the generated API token from `/etc/temms/temms.env`:

```bash
sudo grep '^TEMMS_API_TOKEN=' /etc/temms/temms.env
```

For shell examples on the VM, export it into the current shell:

```bash
export TEMMS_API_TOKEN="$(sudo sed -n 's/^TEMMS_API_TOKEN=//p' /etc/temms/temms.env)"
```

When calling a different VM, such as a central Hub Lite API, use that VM's token:

```bash
export TEMMS_HUB_TOKEN="<token-from-hub-vm-temms.env>"
```

Clients can then use either header:

```bash
curl -H "X-TEMMS-Token: $TEMMS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"conditions":{"environmental.atmospheric.visibility_m":50}}' \
  http://127.0.0.1:8080/v1/control/conditions
```

When `TEMMS_API_TOKEN` is set, Hub Lite routes and Web UI write actions use
the same token check. Read-only UI pages still load, but slot overrides,
condition injection, override clearing, and UI package import require
`X-TEMMS-Token` or a bearer token. UI package import also inherits the daemon's
signed-package policy.

For role-scoped access, set `TEMMS_RBAC_TOKENS` in addition to or instead of
the admin control token. Use comma- or semicolon-separated `role=token` pairs;
repeat a token to give it multiple roles, or use JSON mapping tokens to role
lists. TEMMS never prints these tokens in doctor output.

```bash
export TEMMS_RBAC_TOKENS="operator=op-token;approver=approve-token;edge=edge-token;auditor=audit-token"
```

When RBAC tokens are configured, Hub/API/UI writes require the matching role:
`operator` can package, assign, override, roll back, and export air-gap bundles;
`approver` can approve gated rollouts; `edge` can heartbeat, publish runtime
evidence, update rollout state, and apply local rollouts; `auditor` can export
evidence summaries. `TEMMS_API_TOKEN`, when configured, remains an admin token.

## 9. Telemetry export

For disconnected deployments, export buffered telemetry after the VM is back in range:

```bash
curl -X POST \
  -H "X-TEMMS-Token: $TEMMS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"limit": 5000}' \
  http://127.0.0.1:8080/v1/control/telemetry/export > telemetry-bundle.json
```

Replay that bundle into Hub Lite for central post-mission review, then clear the local edge buffer after transfer:

```bash
temms hub replay-telemetry telemetry-bundle.json \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN" \
  --device-id edge-1 \
  --actor operator:post-mission

curl -X POST \
  -H "X-TEMMS-Token: $TEMMS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"clear": true}' \
  http://127.0.0.1:8080/v1/control/telemetry/replay
```

## 10. Optional Hub Lite online sync

The daemon refreshes its local Hub Lite heartbeat/inventory/deployment status
even when no central Hub is configured. Set `TEMMS_EDGE_HEARTBEAT_INTERVAL_S`
to tune how often local runtime, resource, and deployment telemetry is
refreshed for readiness gates. Set `TEMMS_HUB_URL` when an edge VM should also
sync to a central Hub Lite API while it has network access. The edge agent uses
the same detected inventory upstream, mirrors assigned rollouts for its
`TEMMS_DEVICE_ID`, downloads package archives for assigned rollouts into its
local Hub Lite package cache, and pushes local rollout state changes back to
the hub.

Repeated syncs preserve a valid local package artifact when its registered `source_sha256` still matches the central catalog. If the central package source changes or the cached artifact digest drifts, the edge fetches a fresh artifact before apply.
Heartbeat inventory includes the detected device profile, runtime availability, ONNX providers, and accelerators. Hub Lite uses that inventory when assigning rollouts, so a package that requires TensorRT, CUDA, or TFLite is rejected before it is sent to an incompatible VM.

```ini
TEMMS_HUB_URL=http://hub-vm:8080
TEMMS_HUB_TOKEN=change-me
TEMMS_DEVICE_ID=edge-1
TEMMS_DEVICE_PROFILE=x86_64-cpu
TEMMS_EDGE_HEARTBEAT_INTERVAL_S=60
TEMMS_HUB_SYNC_INTERVAL_S=30
```

If `TEMMS_HUB_URL` is unset, the VM stays fully local and can still use Hub Lite air-gap export/import bundles.

To make a VM execute assigned rollouts automatically, opt in explicitly:

```ini
TEMMS_HUB_AUTO_APPLY=true
TEMMS_ROLLOUT_REQUIRE_SIGNATURE=true
TEMMS_PACKAGE_SIGNING_KEY_FILE=/etc/temms/hub-signing.key
```

With auto-apply enabled, the edge agent applies only rollouts in `assigned` state that were mirrored for its `TEMMS_DEVICE_ID`. The rollout package is still imported through the normal verified package path, policies are promoted into the active policy directory, runtime/device constraints are checked, apply-time readiness preflight verifies pinned runtime evidence plus declared SLO/resource envelopes, the target slot is activated, and the edge replays the local rollout lifecycle back to Hub Lite so central history includes `downloading`, `imported`, and `activated` transitions. If readiness blocks auto-apply, the rollout remains assigned and `rollout.auto_apply_failed` telemetry records `failure_kind`, `blocking_gates`, and the readiness selection.

The same signature settings are inherited by manual Hub Lite API calls on a daemon-backed VM, including package artifact registration and rollout apply. Rollout assignment also rejects package catalog entries without verified signature metadata when signatures are required. This keeps operator-driven recovery paths on the same verification policy as automatic sync.

For USB-style transfer, export a Hub Lite bundle that includes package archives from the central VM:

```bash
temms hub enroll \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN" \
  --device-id edge-1 \
  --device-profile x86_64-cpu

temms hub export \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN" \
  --include-packages \
  --output hub-lite-package-bundle.json
```

Import that bundle on the edge VM:

```bash
temms hub import hub-lite-package-bundle.json \
  --hub-url http://edge-vm:8080 \
  --token "$TEMMS_API_TOKEN"
```

The edge VM writes embedded package artifacts under its local Hub Lite `packages/` directory and rewrites package catalog paths before rollout apply. Evidence and local Hub Lite metadata keep both the registered `source_sha256` and the transferred artifact SHA256, which can differ when a directory package is distributed as a `.temms.tar.zst` archive.

Hub Lite re-checks the cataloged source SHA256 before serving online package downloads or embedding packages into an air-gap bundle. If a package file or directory changes after registration, distribution fails and the operator must re-register the package. Air-gap imports are safe to repeat: missing records are added, newer incoming records replace older local records, and stale bundles do not downgrade newer local rollout, deployment, or package artifact state.

## Device Profiles

Use these initial labels when planning package compatibility:

| Profile | Target | Runtime baseline |
| --- | --- | --- |
| `x86_64-cpu` | Generic VM or mini PC | ONNX Runtime CPU |
| `arm64-jetson` | Jetson Nano/Orin family | ONNX Runtime, TensorRT optional |
| `rpi5-tflite` | Raspberry Pi 5 | TFLite or ONNX Runtime CPU |
| `orin-tensorrt` | Jetson Orin optimized deployment | TensorRT engine preferred |

The edge agent should reject or warn on model packages that declare incompatible runtime constraints for the local device profile.

`temms doctor` prints the detected profile and the known MVP profile list. TEMMS also normalizes common aliases such as `amd64-cpu`, `aarch64-jetson`, `raspberry_pi_5`, and `jetson-orin` before compatibility checks. When package metadata does not set runtime provider options, these profiles supply execution defaults: CPU VMs stay on `CPUExecutionProvider`, Jetson devices prefer CUDA-capable ONNX providers, Orin prefers TensorRT then CUDA then CPU, and Raspberry Pi 5 defaults TFLite to four threads.

## Multi-Arch Container Build

For containerized VMs, TEMMS includes a Buildx Bake definition for `linux/amd64` and `linux/arm64`:

```bash
make docker-buildx
```

The target is defined in `docker-bake.hcl` as `temms-agent`. Use normal Buildx flags to push or retag for your registry, for example:

```bash
docker buildx bake -f docker-bake.hcl --push
```

The image runs the agent as the non-root `temms` user. Runtime-writable paths are limited to `/var/lib/temms`, `/etc/temms`, and the packaged examples used by the local simulation entrypoint.

By default, the Dockerfile installs `TEMMS_EXTRAS=inference`, which keeps
production and acceptance edge images free of MLflow and simulation-only
packages. The local `docker-compose.yml` simulation opts into
`TEMMS_EXTRAS=sim` so MLflow development workflows still work when you run
`make docker-up`. Override the build arg only when the target image really
needs those extras:

```bash
docker build --build-arg TEMMS_EXTRAS=inference -t temms/agent:edge .
docker build --build-arg TEMMS_EXTRAS=sim -t temms/agent:sim .
```

For local Hub runtime-target validation, build the default x86 runtime image tag that Hub Lite already knows about:

```bash
make docker-build-runtime
```

This creates `temms/agent:inference-amd64`, the image used by the built-in `temms-x86_64-cpu` runtime target. The container entrypoint passes explicit commands through, so `docker run temms/agent:inference-amd64 temms package validate ...` executes package validation instead of launching the daemon.

For a three-agent container rehearsal before provisioning VMs, start one Hub Lite agent and two edge agents:

```bash
TEMMS_ACCEPTANCE_PACKAGE_DIR=./dist \
TEMMS_ACCEPTANCE_TOKEN=change-me \
TEMMS_ACCEPTANCE_SIGNING_KEY=change-me \
make docker-acceptance-up
```

Docker Desktop or a Docker daemon with the Compose plugin must be running before these targets are invoked. The acceptance signing key is injected into the Hub and edge daemon environments for verification; the harness does not send it in rollout request bodies.

This uses `deploy/docker-compose.acceptance.yml` and exposes:

- Hub: `http://localhost:18080`
- Online edge: `http://localhost:18081`
- Air-gap edge: `http://localhost:18082`

The central container mounts `${TEMMS_ACCEPTANCE_PACKAGE_DIR}` at `/acceptance-packages`, so pass package paths like `/acceptance-packages/pkg-online-x86.temms.tar.zst` to the multi-VM harness. Stop the rehearsal with:

```bash
make docker-acceptance-down
```

To run the whole container rehearsal in one command, including synthetic signed packages, health waits, the multi-VM harness, and `acceptance-summary.json` generation:

```bash
make docker-acceptance
```

Set `KEEP_CONTAINERS=true` if you want the three agents left running for inspection after the harness completes.

## Local MVP Smoke and Acceptance Tests

Before trying the flow on separate VMs, run the local Hub Lite smoke tests from a checkout:

```bash
make mvp-smoke
```

This exercises both MVP distribution paths. The air-gap test covers signed package archive, Hub registration, device enrollment, rollout assignment, bundle export with package artifacts, edge bundle import, rollout apply, model cache import, and slot activation. The online test starts separate local Hub and edge HTTP APIs, downloads the assigned package artifact into the edge package cache, auto-applies the rollout, and pushes the activated state back to Hub Lite.

For the clearest preflight check before a real multi-VM trial, run the acceptance target:

```bash
make mvp-acceptance
```

The acceptance flow runs one central Hub Lite instance plus two independent edge agents with different profiles: `x86_64-cpu` online sync and `rpi5-tflite` air-gap import. It verifies profile compatibility blocking, signed package download/import, rollout activation, rollback to a previous known-good model, central deployment status, and evidence bundle export from each edge.

## Real Multi-VM Acceptance Harness

After installing one Hub VM and two edge VMs, use the shell harness from the checkout or release bundle to run the same acceptance shape against real agents:

```bash
HUB_URL=http://hub-vm:8080 \
ONLINE_EDGE_URL=http://edge-online:8080 \
AIRGAP_EDGE_URL=http://edge-airgap:8080 \
ONLINE_PACKAGE_ID=pkg-online-x86 \
ONLINE_PACKAGE_PATH=/var/lib/temms/packages/pkg-online-x86.temms.tar.zst \
AIRGAP_PACKAGE_ID=pkg-airgap-rpi \
AIRGAP_PACKAGE_PATH=/var/lib/temms/packages/pkg-airgap-rpi.temms.tar.zst \
AUTH_TOKEN=change-me \
deploy/multi-vm-acceptance.sh connected-lab
```

Run this from the Hub VM when package paths are local to the Hub filesystem. The Hub VM and both edge VMs should have `TEMMS_ROLLOUT_REQUIRE_SIGNATURE=true` plus `TEMMS_PACKAGE_SIGNING_KEY_FILE=/etc/temms/hub-signing.key` configured before the harness starts. The online edge should also have `TEMMS_HUB_URL`, `TEMMS_DEVICE_ID=edge-online`, `TEMMS_DEVICE_PROFILE=x86_64-cpu`, and `TEMMS_HUB_AUTO_APPLY=true`.

For a true disconnected transfer, split the air-gap leg:

```bash
# On the Hub VM
deploy/multi-vm-acceptance.sh export-airgap

# Carry ./temms-mvp-acceptance/hub-lite-airgap-bundle.json to the edge VM, then run:
AIRGAP_EDGE_URL=http://127.0.0.1:8080 \
AIRGAP_PACKAGE_ID=pkg-airgap-rpi \
deploy/multi-vm-acceptance.sh import-airgap
```

The harness writes `acceptance-summary.json`, `central-deployment-status.json`, `online-edge-evidence.json`, `airgap-edge-evidence.json`, and the exported air-gap bundle under `./temms-mvp-acceptance` by default. The summary file records the run mode, device profiles, package IDs, rollout IDs, final rollout states, and paths to evidence artifacts. Each edge evidence bundle includes doctor-style diagnostics, model cache health, runtime capabilities, rollout history, telemetry, and decision audit records. Those files are the operator evidence that the MVP can deploy, activate, roll back, and audit models across multiple edge VMs.
