# Architecture

## Three-Tier Design

TEMMS uses a three-tier architecture. Each tier can run independently and offline.

```
┌──────────────────────────────────────────────────────────┐
│  Tier 1: MLflow                                          │
│  Standard model registry. Not modified. Can be local or  │
│  cloud. Data scientists use the UI they already know.    │
└──────────────────────┬───────────────────────────────────┘
                       │ Pull models via MLflow API
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Tier 2: TEMMS Hub (future)                              │
│  Packages models for edge consumption. Delta updates,    │
│  fleet management, air-gap export to USB.                │
└──────────────────────┬───────────────────────────────────┘
                       │ Push packages (network, USB, SD card)
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Tier 3: TEMMS Daemon (this repo)                        │
│  Edge runtime. Receives packages, manages local cache,   │
│  evaluates policies, switches models, serves inference.  │
│  Runs with ZERO network dependencies.                    │
└──────────────────────────────────────────────────────────┘
```

You only need Tier 3 to get started. Tier 1 (MLflow) is optional for local development. Tier 2 (Hub) is planned.

## TEMMS Daemon Internals

The daemon is a single async Python process. No microservices, no Kubernetes — edge devices need simplicity.

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

## Policy Evaluation

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

**Single process, no microservices.** Edge devices have 4GB RAM. We can't afford Docker-in-Docker or K3s overhead. One Python process does everything.

**SQLite, not Postgres.** Zero dependencies, embedded, works offline. WAL mode for concurrent reads during inference.

**YAML policies, not code.** Policies are data, not logic. They can be version-controlled, reviewed, and pushed to devices without code changes.

**Hot-swap, not restart.** Restarting a model loader takes seconds and drops requests. Hot-swap takes milliseconds and drops nothing.

**Operator override is king.** In safety-critical systems, the human must be able to override any automated decision. TEMMS enforces this at the architecture level — operator commands have priority 1000, policy decisions have priority ≤ 999.

**Log everything.** Every model switch is logged with the full condition snapshot. This enables post-mission analysis: "Why did the system switch to thermal at 14:32?" Answer: visibility was 15m, mission priority was critical, fog-conditions rule matched.
