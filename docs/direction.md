# TEMMS Direction — Optimized Edge Model Management for DDIL

**North star:** TEMMS is the deterministic, auditable **control plane** for running
and switching models on edge devices in **DDIL** environments (Denied, Disrupted,
Intermittent, Limited connectivity). It is not a model server, not a registry,
and not a crypto library — it is the thin layer that makes those pieces behave
correctly, provably, and autonomously when the network is gone.

## The one sentence

`model registry → signed portfolio → any target/runtime → DDIL-aware
best-feasible selection → load into device`, with an evidence chain proving
*which model ran, when, why it was the best feasible choice, and under what
authority.*

The core model is [best-feasible model control](model-control.md): a device runs
a **portfolio** of models, each declaring the conditions it is *for* and the
resources it *requires*, and TEMMS keeps the best *feasible* one serving —
selection and degradation are one deterministic mechanism, not a primary with a
fallback list, and the model's own output feeds back into the decision. This
supersedes the earlier `default_model` + `fallback_chain` framing.

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

## Validation environment (now: a Mac; hardware later)

Everything must be **validatable on a single Apple Silicon Mac (arm64, ~16 GB
RAM)** for now. Real edge hardware comes only after the behavior is simulated
exhaustively. This is a hard constraint on what we build and how we prove it:

- **Real inference** runs in-process on **ONNX Runtime** (CoreML + CPU execution
  providers on this Mac) — the reference adapter. This is the near-term backend.
- **Deterministic behavior/DDIL logic** runs on the simulation runtime
  (`TEMMS_INFERENCE_SIMULATE_RUNTIME=1`) — concurrency, swaps, offline/replay,
  hysteresis, recovery — with no real model quirks.
- **Multi-edge / fleet DDIL scenarios** run as Docker containers (hub + edge
  agents) on the same Mac. No physical fleet required to exercise rollout,
  air-gap bundles, or reconnect.
- **Fault injection** is host-level and Mac-runnable: `kill -9`, disk-full on
  evidence/state writes, clock jumps, flapping/garbage sensor values. See
  [reliability](reliability.md) for the soak and crash-atomicity harnesses.
- **Pi-class (arm64) targets** are validated natively: Apple Silicon *is*
  arm64, so `make docker-acceptance-arm64` runs the edge agent pinned to
  `linux/arm64` and asserts the container is genuinely `aarch64`, that the agent
  infers an arm64 profile from the silicon, and that a declared Pi-class profile
  does not contradict the hardware. No board, no emulation. Jetson/GPU stays
  deferred to real hardware.

Implications:
- **Triton is not validatable on macOS/arm64** — no Mac build. It stays a
  documented adapter seam, built only when a Linux/Jetson target is real. In-
  process ORT is what we prove now.
- **16 GB RAM** means large-model (multi-GB/VLA) behavior is *simulated* via the
  `fits()` capacity abstraction and small stand-in models — never validated with
  real multi-GB artifacts on this box. Reinforces descoping the bespoke
  footprint estimator.
- The bar for "done" on any DDIL feature is a **repeatable single-Mac
  simulation** that exercises it (ideally in the soak/chaos harness, #13), not a
  hardware demo.

## Litmus test for any new work

Before building anything, ask: *does a mature tool already do this, and does it
work in a disconnected edge context?* If yes → integrate it behind an adapter.
If it exists but assumes connectivity/datacenter → that gap is ours to fill.
If nothing exists → build it, minimally.
