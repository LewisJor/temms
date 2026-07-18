# Demo Runbook

A repeatable, single-Mac walkthrough of TEMMS as a DDIL edge model management
system: **signed model → deploy → adaptive edge → offline/DDIL → provable,
tamper-evident evidence.** Everything runs in the local Docker stack; no
hardware, GPU, or external services.

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
- MLflow: <http://localhost:5001>

## 1. The Hub (signed inventory)

Open **<http://localhost:8080/ui/hub>** — the Mission Package Workbench. The
seeded package (`pkg-vision-models-20240115`) is **Ed25519-signed**; the runtime
fit shows `edge-sim / 95/100 optimal`.

## 2. Deploy to the edge (proof-gated)

In the workbench: **Plan package** → open **Edge Deploy** → **Stage rollout**.
The stage gate passes and the rollout is assigned to `edge-sim` with the full
proof chain (package identity, edge handoff, mission contract, capability lock,
runtime plan, deployment intent digests).

CLI mirror:

```bash
python scripts/mission_package_smoke.py --hub-url http://localhost:8080
```

## 3. Adaptive swap under changing conditions

Inject fog; the policy engine hot-swaps to the low-light model (old model keeps
serving until the new one is warmed; `min_dwell_s` hysteresis prevents thrash on
a flapping sensor):

```bash
curl -s -X POST http://localhost:8080/v1/control/conditions \
  -H "Content-Type: application/json" \
  -d '{"conditions":{"environmental.atmospheric.visibility_m":40,"environmental.atmospheric.precipitation":"fog"}}'
```

Each activation appends a link to the tamper-evident decision chain.

## 4. DDIL: operate offline, then reconnect

```bash
curl -s -X POST http://localhost:8080/v1/control/offline   # comms lost
# ... the edge keeps serving and queues signed intents ...
curl -s -X POST http://localhost:8080/v1/control/online    # comms restored -> replay/sync
```

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
