# Demo Runbook

A repeatable, single-Mac walkthrough of TEMMS as a DDIL edge model management
system: **signed model → deploy → adaptive edge → offline/DDIL → provable,
tamper-evident evidence.** Everything runs in the local Docker stack; no
hardware, GPU, or external services.

Every step below includes a **Show** command, so each claim is visible on screen
rather than asserted.

## 0. Bring up a clean stack (~1–2 min)

```bash
make docker-clean && make docker-up
# wait for health:
until curl -sf http://localhost:8080/v1/health >/dev/null; do sleep 2; done
until [ "$(curl -s http://localhost:5001/health)" = OK ]; do sleep 2; done
```

The daemon seeds a signed, released example package, an online `edge-sim`
device, and **generates an Ed25519 demo signing keypair** (real asymmetric,
offline-verifiable provenance) into the data volume.

- Hub UI: <http://localhost:8080/ui/hub>
- **DDIL dashboard: <http://localhost:3000/d/temms-ddil>** (opens directly, no login)
- MLflow: <http://localhost:5001>

Keep this handy — it prints the active model on the `vision` slot:

```bash
active() { curl -s http://localhost:8080/v1/slots/vision/status \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["active_model"])'; }
```

## 1. The Hub (signed inventory)

Open **<http://localhost:8080/ui/hub>** — the Mission Package Workbench. The
seeded package (`pkg-vision-models-20240115`) is promoted through
`candidate → validated → approved → released`, and the runtime fit shows
`edge-sim / 95/100 optimal`.

## 2. Deploy to the edge (proof-gated)

In the workbench: **Plan package** → open **Edge Deploy** → **Stage rollout**.
The stage gate passes and the rollout is assigned to `edge-sim` with the full
proof chain (package identity, edge handoff, mission contract, capability lock,
runtime plan, deployment intent digests).

CLI mirror:

```bash
python scripts/mission_package_smoke.py --hub-url http://localhost:8080
# -> stage_gate_status: passed, runtime_fit_score: 95, apply_state: activated
```

## 3. Adaptive swap under changing conditions

Open the **DDIL dashboard** (<http://localhost:3000/d/temms-ddil>) alongside the
terminal — *Model activations* and *Decision chain length* step up live as you
run this.

Degrade the environment. Visibility collapses on a **critical** mission, so the
policy engine hot-swaps to the lightweight model (the old model keeps serving
until the new one is warmed; `min_dwell_s` hysteresis prevents thrash on a
flapping sensor):

```bash
active   # -> model-yolov8-lowlight-001  (or daylight, depending on time of day)

curl -s -X POST http://localhost:8080/v1/control/conditions \
  -H "Content-Type: application/json" \
  -d '{"conditions":{"environmental.atmospheric.visibility_m":15,"operational.mission.priority":"critical"}}'

sleep 5; active   # -> model-mobilenet-tiny-001   ← swapped
```

Then recover, and it swaps back:

```bash
curl -s -X POST http://localhost:8080/v1/control/conditions \
  -H "Content-Type: application/json" \
  -d '{"conditions":{"environmental.atmospheric.visibility_m":10000,"operational.mission.priority":"routine"}}'

sleep 5; active   # -> back to the standard model
```

**Show** — every activation appends a link to the tamper-evident decision chain:

```bash
curl -sL http://localhost:8080/metrics \
  | grep -E "^temms_(model_swaps_total|decision_chain_length|swap_latency_ms_count) "
```

> **Why this trigger, and not fog?** The `critical-low-visibility` rule switches
> to a *third* model (`mobilenet-tiny`), so the swap is visible whatever the
> hour. The `fog-conditions` and `night-operations` rules both target
> `yolov8-lowlight` — so after dark, `night-operations` has already selected it
> and injecting fog is a correct but **invisible** no-op. Use fog for a daytime
> demo only.

## 4. DDIL: operate offline, then reconnect

```bash
curl -s -X POST http://localhost:8080/v1/control/offline   # comms lost
```

**Show** — the edge is offline but still deciding autonomously. Trigger a
condition change while disconnected and watch the chain grow and an intent
queue:

```bash
curl -s -X POST http://localhost:8080/v1/control/conditions \
  -H "Content-Type: application/json" \
  -d '{"conditions":{"environmental.atmospheric.visibility_m":15,"operational.mission.priority":"critical"}}'

sleep 5
curl -sL http://localhost:8080/metrics \
  | grep -E "^temms_(offline_mode|pending_intents|decision_chain_length) "
# -> offline_mode 1, pending_intents 1, chain grew
```

Inference keeps serving throughout:

```bash
curl -s -o /dev/null -w "infer HTTP %{http_code}\n" \
  -X POST http://localhost:8080/v1/slots/vision/infer -F "file=@your-frame.jpg"
```

Restore comms and **replay the queued signed intents**:

```bash
curl -s -X POST http://localhost:8080/v1/control/online   # comms restored
curl -s -X POST http://localhost:8080/v1/control/sync     # replay the queue
# -> replayed: 1, pending_cleared: 1, signature_required: true
```

> The local demo stack sets no `TEMMS_HUB_URL`, so replay is triggered
> explicitly rather than on a background sync timer. The `sync` response is
> worth showing: it carries the preflight status, signature state, and payload
> digest for each queued intent.

## 5. The moat: prove the evidence offline

Export the evidence and verify the signed, tamper-evident decision chain with the
**public key only** — the record of *which model ran, when, and why* holds up
even on a captured device.

```bash
scripts/verify-provenance-demo.sh
```

This extracts the daemon's public key, exports the evidence, and:

1. **Verifies** the chain + head signature → *Decision chain intact / Head
   signature verified*.
2. **Tampers** with one decision and re-verifies → *Decision chain BROKEN at
   entry N* (exit code 2).

## 6. Reset

```bash
make docker-clean
```

## Talking points

- **Integrate the commodity, build the differentiator:** ONNX Runtime serves,
  MLflow is the registry — TEMMS owns the DDIL policy, provenance, and evidence.
- **Offline-first provenance:** Ed25519 signatures verify with a provisioned
  public key, no CA or transparency log (unusable under DDIL).
- **Deterministic + auditable:** policy-bound switching (no LLM in the loop) and
  a hash-linked, signed decision chain — the safety-case story.
- **Swaps are cheap:** the dashboard's swap-latency panel shows load → warm →
  activate in tens of milliseconds, with inference errors flat at zero across
  every swap.
- **A bad frame is not a bad model:** a corrupt or unsupported frame is rejected
  `400` and counted as `temms_invalid_input_total`. It never marks the slot
  unhealthy and never triggers the fallback chain — a degraded sensor cannot
  down the slot or churn models.

## Endpoint reference

Handy during Q&A — note there is no `GET /v1/slots` collection route:

| Purpose | Endpoint |
|---|---|
| Slot state / active model | `GET /v1/slots/{slot}/status` |
| Inference (multipart image) | `POST /v1/slots/{slot}/infer` (undecodable frame → `400`) |
| Inject conditions | `POST /v1/control/conditions` |
| Offline / online | `POST /v1/control/offline` · `/v1/control/online` |
| Replay queued intents | `POST /v1/control/sync` |
| Decision timeline | `GET /v1/control/audit/timeline` |
| Metrics | `GET /metrics` (307 → follow with `curl -sL`) |
