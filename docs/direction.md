# TEMMS Direction — Optimized Edge Model Management for DDIL

**North star:** TEMMS is the deterministic, auditable **control plane** for running
and switching models on edge devices in **DDIL** environments (Denied, Disrupted,
Intermittent, Limited connectivity). It is not a model server, not a registry,
and not a crypto library — it is the thin layer that makes those pieces behave
correctly, provably, and autonomously when the network is gone.

## The one sentence

`model registry → any target/runtime → signed → DDIL-aware policy → load into device`,
with an evidence chain proving *which model ran, when, why, and under what
authority.*

## Design principle: integrate the commodity, build the differentiator

TEMMS must not reimplement solved problems. Every layer is either **integrated**
(a mature tool, wrapped behind a thin adapter) or **built** (because nothing
existing fits the DDIL/edge/provenance constraint).

| Layer | Decision | Why |
|-------|----------|-----|
| Model serving + hot-swap | **Integrate** — ONNX Runtime (in-process) now; Triton-class server later, behind one adapter | Serving and reload are commodities. ORT is a library (we own a ~40-line swap); Triton is a server (swap is its API). Either way, thin adapter — not a bespoke runtime. |
| Model registry / promotion | **Integrate** — MLflow | Already wired. Registry + versioning is solved. |
| Package signing | **Build (thin, on a vetted primitive)** — Ed25519 via `cryptography` | The *primitive* is off-the-shelf; the DDIL story is ours. Sigstore/cosign assume an online CA + transparency log — the wrong model for disconnected verification. TEMMS verifies **offline** against provisioned public keys. |
| Fleet rollout / edge deploy | **Integrate where possible**, keep our surface minimal | IoT-edge frameworks exist; we only own the DDIL-aware rollout semantics. |
| **Condition → policy → model decision** | **Build** | Nothing off-the-shelf switches models deterministically from live sensor/environment conditions. This is core. |
| **DDIL state machine** (offline operation, signed-intent queue, replay on reconnect) | **Build** | The defining capability. No existing tool does autonomous, auditable operation through comms loss. |
| **Evidence chain** (which model, when, why, under what proof) | **Build** | The audit/provenance record that ties policy + provenance + swaps together. The product's proof. |

## What this means concretely

**Build (the irreducible core):**
1. Deterministic condition→policy→model decision engine (incl. anti-flap hysteresis).
2. DDIL state machine: offline mode, signed-intent queue, deterministic replay on reconnect.
3. Offline-verifiable signed provenance + evidence chain.
4. A **thin runtime adapter** interface so any target/runtime can be driven
   (in-process ORT today; Triton/TFLite/TensorRT behind the same seam).

**Integrate (do not rebuild):** ONNX Runtime / Triton (serving+swap), MLflow
(registry), `cryptography` Ed25519 (signing primitive).

**Shrink / delete (redundant with the above or premature):**
- Bespoke hot-swap machinery beyond the minimal adapter primitive (see #12).
- Hub/fleet "proof-chain" ceremony in `hub_lite.py` that duplicates
  registry/fleet-manager concerns — shrink to the DDIL core before decomposing (#16).
- Native-port-driven spec ceremony and multi-GB speculative generality — deferred
  until a real integration target exists (#18, #19).

## Non-goals

- TEMMS is **not agentic**. No LLM/VLA ever makes a switching decision. Switching
  is policy-bound and deterministic — that determinism is the safety-case and
  certification story. VLAs, if integrated, are just large models in a slot whose
  tier is chosen by policy (#19).
- TEMMS does **not** compete with Triton/TF Serving on serving throughput. It
  orchestrates them.

## Litmus test for any new work

Before building anything, ask: *does a mature tool already do this, and does it
work in a disconnected edge context?* If yes → integrate it behind an adapter.
If it exists but assumes connectivity/datacenter → that gap is ours to fill.
If nothing exists → build it, minimally.
