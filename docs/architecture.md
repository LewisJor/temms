# Architecture

```{note}
This document describes the **current** implementation: two async loops, a
condition store, and a `default_model` + `fallback_chain` selection model. The
target design that supersedes the fallback-chain approach is
[best-feasible model control](model-control.md) — one deterministic mechanism for
selection *and* degradation, with the model's output feeding back into the
decision. Sections below marked *(being superseded)* are on that path.
```

## System Context

TEMMS has a hub-and-daemon architecture. The daemon runs on the edge node. Hub
is the upstream tool for managing candidate models, packaging them with policy
metadata, signing artifacts, and running targeted container tests for the
runtimes or device profiles that will consume them.

```
┌──────────────────────────────────────────────────────────┐
│  Model sources                                           │
│  Local files, registry exports, training pipelines,      │
│  agent-generated models, or external build systems.      │
└──────────────────────┬───────────────────────────────────┘
                       │ Candidate models and metadata
                       ▼
┌──────────────────────────────────────────────────────────┐
│  TEMMS Hub                                               │
│  Model inventory, package assembly, artifact signing,    │
│  targeted container tests, compatibility evidence.       │
└──────────────────────┬───────────────────────────────────┘
                       │ Signed packages and policies
                       │ over network or removable media
                       ▼
┌──────────────────────────────────────────────────────────┐
│  TEMMS daemon                                            │
│  Imports packages, manages the local model cache,        │
│  evaluates policies, switches models, serves inference,  │
│  and records decision evidence.                          │
└──────────────────────────────────────────────────────────┘
```

The daemon does not require Hub or upstream connectivity to evaluate policies or
serve inference once packages and policies are available locally. Hub improves
the artifact handoff before deployment; it is not required for local model
selection after a package is imported.

TEMMS does not implement model training, labeling, experiment tracking, or
general fleet orchestration. Hub owns package preparation and validation;
the daemon owns the edge-node runtime: conditions, policies, model activation,
fallback, operator override, and decision evidence.

## TEMMS Daemon Internals

The daemon is a single async Python process. It avoids external service
dependencies so it can run on small edge devices and continue operating while
disconnected.

```
┌─────────────────────────────────────────────────────────┐
│                     TEMMSDaemon                          │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Condition    │  │ Policy       │  │ Inference      │  │
│  │ Loop         │  │ Loop         │  │ Server         │  │
│  │              │  │              │  │ (FastAPI)      │  │
│  │ Collects:    │  │ Evaluates:   │  │                │  │
│  │ • CPU temp   │  │ • YAML rules │  │ Endpoints:     │  │
│  │ • Memory     │──│ • Priorities │  │ • /infer       │  │
│  │ • Time       │  │ • Overrides  │──│ • /status      │  │
│  │ • Operator   │  │              │  │ • /control     │  │
│  │   injection  │  │ Executes:    │  │ • /ui          │  │
│  │              │  │ • Hot-swap   │  │                │  │
│  └──────┬───────┘  │ • Fallback   │  └───────────────┘  │
│         │          └──────────────┘                      │
│         ▼                                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │            ConditionStore (SQLite)                │   │
│  │  path                    | value  | source | pri │   │
│  │  weather.visibility_m    | 80     | sensor | 100 │   │
│  │  platform.cpu_temp_c     | 62     | sensor | 100 │   │
│  │  weather.visibility_m    | 50     | oper.  | 1000│   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Slot Manager                         │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │   │
│  │  │ vision   │  │ targeting│  │ nav      │       │   │
│  │  │ yolov8-  │  │ rgb-     │  │ lidar-   │       │   │
│  │  │ daylight │  │ tracker  │  │ slam     │       │   │
│  │  │ RUNNING  │  │ RUNNING  │  │ STOPPED  │       │   │
│  │  └──────────┘  └──────────┘  └──────────┘       │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### The Two Loops

The daemon runs two async loops:

**Condition Loop** (every 5s by default):
1. Runs all registered collectors concurrently
2. Stores results in ConditionStore with source and priority
3. Signals the policy loop that conditions changed

**Policy Loop** (event-driven + 1s fallback):
1. Triggered by condition changes OR periodic timer
2. For each running slot:
   - Check if operator override is active → skip if yes
   - Evaluate policies by priority (highest wins)
   - If matched rule says different model → execute switch
3. Hot-swap: load new model, atomic pointer swap, unload old
4. Log decision with full condition snapshot

### Data Flow

```
Sensor / Operator / Derived
         │
         ▼
  ConditionStore.set(path, value, source, priority)
         │
         ▼ (conditions_changed event)
  PolicyEngine.evaluate_slot("vision")
         │
         ▼ (if switch needed)
  InferenceRuntime.load_model("vision", "yolov8-lowlight")
         │
         ▼ (atomic swap)
  SlotManager.activate_model(slot, model, trigger, conditions)
         │
         ▼ (logged)
  decision_log: {slot, from, to, trigger, conditions_snapshot, timestamp}
```

## Model Hot-Swap

The inference runtime uses copy-on-read locking for zero-downtime model switching:

```python
# Thread 1: Inference request
with slot_runtime.lock:           # Brief lock — grab reference
    loaded = slot_runtime.loaded_model
# Run inference WITHOUT holding the lock
outputs = loaded.runtime.infer(inputs)

# Thread 2: Hot-swap (concurrent)
new_model = loader.load(new_path)  # Load OUTSIDE the lock
with slot_runtime.lock:            # Brief lock — atomic swap
    old = slot_runtime.loaded_model
    slot_runtime.loaded_model = new_model
old.runtime.unload()               # Unload OUTSIDE the lock
```

In-flight requests complete on the old model. New requests go to the new model. No requests are dropped.

## Condition Priority System

Multiple sources can set the same condition. The highest priority wins.

| Priority | Source | Example |
|----------|--------|---------|
| 1000 | Operator override | `temms condition set vis 50` |
| 100 | Onboard sensors | CPU temp from `/sys/class/thermal/` |
| 90 | Derived/computed | Time of day, sun position |
| 50 | External data | Weather API (when connected) |
| 10 | Cached | Last-known-good from disk |

An operator override (priority 1000) always wins over sensor data (100). This is intentional — the human in the loop has final authority.

## Policy Evaluation *(being superseded)*

The rule-and-fallback-chain evaluation below is the current implementation.
[Best-feasible model control](model-control.md) replaces it: selection over an
operating point (conditions × resources × connectivity × observed performance),
with degradation emerging from feasibility rather than a separate `fallback_chain`.

Policies are slot-scoped YAML files. Each policy has rules sorted by priority.

```
For each running slot:
  1. Is there an active operator override?
     → Yes: skip (operator wins)
     → No: continue

  2. Collect all policies for this slot

  3. Evaluate rules in priority order (highest first):
     - Check conditions (all/any combinators)
     - Check min_confidence thresholds
     - First match wins

  4. If matched model != current model:
     → Execute hot-swap
     → Log decision

  5. If no rules match:
     → Keep current model (no change)

  6. If model load fails:
     → Execute fallback chain
     → Log fallback decision
```

## Storage Layout

```
/var/lib/temms/
├── temms.db              # SQLite — slots, conditions, decisions, models
├── models/               # Model files on disk
│   ├── model-yolov8-daylight-001/
│   │   └── yolov8n-daylight.onnx
│   ├── model-yolov8-lowlight-001/
│   │   └── yolov8n-lowlight.onnx
│   └── model-mobilenet-tiny-001/
│       └── mobilenet-v2-tiny.onnx
└── telemetry/            # Buffered telemetry (future)

/etc/temms/
├── temms.yaml            # Daemon configuration
└── policies/             # Active policy files
    ├── weather-adaptive.yaml
    └── thermal-adaptive.yaml
```

## Key Design Decisions

**Single process.** One Python process manages local state, policy evaluation,
model activation, inference serving, and the control API.

**SQLite, not Postgres.** Zero dependencies, embedded, works offline. WAL mode for concurrent reads during inference.

**YAML policies, not code.** Policies are data, not logic. They can be version-controlled, reviewed, and pushed to devices without code changes.

**Hot-swap, not restart.** Restarting a model loader takes seconds and drops requests. Hot-swap takes milliseconds and drops nothing.

**Operator override has highest priority.** Operator-provided conditions and
manual model selections are represented with higher priority than collector or
policy-driven updates.

**Log model decisions.** Every model switch is logged with the condition
snapshot and trigger details so the active model can be reconstructed later.
