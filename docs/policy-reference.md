# Policy Reference

Policies are YAML files that tell TEMMS when to switch models. They live in `/etc/temms/policies/` (or wherever your daemon is configured to look).

## Schema

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: <unique-name>              # Required. Used in logs and CLI.
  description: <text>              # Optional. Human-readable description.
spec:
  slot: <slot-name>                # Required. Which slot this policy controls.
  default_model: <model-name>      # Optional. Used when no rules match.

  rules:                           # Required. At least one rule.
    - name: <rule-name>            # Required. Appears in decision logs.
      priority: <integer>          # Required. Higher = evaluated first.
      min_dwell_s: <number>        # Optional. Default 0. Anti-flap hysteresis:
                                   # conditions must hold continuously for this
                                   # many seconds before the rule may switch.
      conditions:                  # Required. When to activate this rule.
        all: [...]                 # All conditions must be true (AND)
        any: [...]                 # At least one must be true (OR)
      action:                      # Required. What to do when rule matches.
        switch_to: <model-name>    # Required. Model to activate.
        preload: [<model>, ...]    # Optional. Pre-load for fast switching.

  allow_operator_override: true    # Optional. Default: true.

  fallback_chain:                  # Optional. Tried in order if primary fails.
    - <model-name>
    - <model-name>
    - <model-name>
```

## Conditions

Each condition check has three required fields:

```yaml
conditions:
  all:  # or "any"
    - metric: <condition-path>     # Dot-separated path
      operator: <comparison>       # See operator list below
      value: <target-value>        # What to compare against
      min_confidence: <0.0-1.0>    # Optional. Skip if confidence too low.
```

### Operators

| Operator | Meaning | Example |
|----------|---------|---------|
| `eq` | Equal | `value: "fog"` |
| `neq` | Not equal | `value: "none"` |
| `gt` | Greater than | `value: 75` |
| `gte` | Greater than or equal | `value: 75` |
| `lt` | Less than | `value: 20` |
| `lte` | Less than or equal | `value: 100` |
| `in` | Value is in list | `value: [fog, mist, haze]` |
| `not_in` | Value is not in list | `value: [clear, sunny]` |
| `matches` | Regex match | `value: "^thermal-.*"` |
| `exists` | Condition exists | (no value needed) |
| `not_exists` | Condition doesn't exist | (no value needed) |

### Combinators

- **`all`**: Every condition must be true (logical AND)
- **`any`**: At least one condition must be true (logical OR)

You can nest them:

```yaml
conditions:
  all:
    - metric: environmental.atmospheric.visibility_m
      operator: lte
      value: 100
    - metric: operational.mission.priority
      operator: in
      value: [high, critical]
```

## Priority

Rules are evaluated in priority order, **highest first**. First match wins.

```yaml
rules:
  - name: critical-override
    priority: 200        # Checked first
    conditions: ...
    action:
      switch_to: emergency-model

  - name: normal-conditions
    priority: 50         # Checked last
    conditions: ...
    action:
      switch_to: standard-model
```

Operator overrides always have implicit priority 1000+ and bypass policy evaluation entirely.

## Common Condition Paths

These are the standard condition paths used throughout TEMMS:

### Environmental

| Path | Type | Description |
|------|------|-------------|
| `environmental.atmospheric.visibility_m` | float | Visibility in meters |
| `environmental.atmospheric.precipitation` | string | none, rain, snow, fog, etc. |
| `environmental.atmospheric.wind_speed_ms` | float | Wind speed in m/s |
| `environmental.atmospheric.temperature_c` | float | Air temperature |
| `environmental.celestial.ambient` | string | bright, normal, low, dark |
| `environmental.celestial.sun_elevation_deg` | float | Sun angle (negative = below horizon) |

### Platform

| Path | Type | Description |
|------|------|-------------|
| `platform.compute.cpu_temp_c` | float | CPU temperature in Celsius |
| `platform.compute.gpu_temp_c` | float | GPU temperature |
| `platform.compute.memory_available_mb` | int | Available RAM |
| `platform.power.battery_pct` | int | Battery percentage (0-100) |
| `platform.power.power_source` | string | battery, tethered, solar |

### Operational

| Path | Type | Description |
|------|------|-------------|
| `operational.mission.phase` | string | patrol, transit, engage, rtb |
| `operational.mission.priority` | string | routine, normal, high, critical |
| `operational.threat.level` | string | none, low, medium, high |
| `operational.connectivity.offline` | bool | True when local offline/DDIL mode is active |
| `operational.connectivity.mode` | string | online or offline |
| `operational.connectivity.network_available` | bool | False when local offline/DDIL mode is active |

### Runtime Collector Health

The daemon publishes collector health alongside collected values, so degraded
local sensors can drive policy decisions even when they fail to report a normal
measurement.

| Path | Type | Description |
|------|------|-------------|
| `runtime.collectors.<collector>.healthy` | bool | False when the named collector failed during the latest collection pass |
| `runtime.collectors.<collector>.last_error` | string/null | Last collection error, or null after a healthy pass |
| `runtime.collectors.<collector>.reported_count` | int | Number of condition values reported by the collector |

### Runtime Inference Health

The inference API publishes per-slot runtime health when an active model fails
during request handling. If the slot policy has a `fallback_chain`, TEMMS tries
that chain, hot-swaps to the first model that loads, retries the request once,
and records the failure in the fallback decision snapshot.

| Path | Type | Description |
|------|------|-------------|
| `runtime.inference.<slot>.healthy` | bool | False after an active inference failure, true after a successful serve |
| `runtime.inference.<slot>.last_error` | string/null | Last active inference error, or null after recovery |
| `runtime.inference.<slot>.failed_model` | string/null | Model id that most recently failed during inference |

## Examples

### Weather-Adaptive Vision

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: weather-adaptive-vision
  description: Switch vision model based on weather conditions
spec:
  slot: vision
  default_model: yolov8-daylight

  rules:
    - name: fog-conditions
      priority: 80
      conditions:
        any:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 100
          - metric: environmental.atmospheric.precipitation
            operator: in
            value: [fog, mist]
      action:
        switch_to: yolov8-lowlight

    - name: night-operations
      priority: 70
      conditions:
        all:
          - metric: environmental.celestial.ambient
            operator: in
            value: [low, dark]
      action:
        switch_to: yolov8-lowlight

    - name: critical-low-visibility
      priority: 150
      conditions:
        all:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 20
          - metric: operational.mission.priority
            operator: eq
            value: critical
      action:
        switch_to: mobilenet-tiny

  allow_operator_override: true

  fallback_chain:
    - yolov8-daylight
    - yolov8-lowlight
    - mobilenet-tiny
```

### Thermal Throttling

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: thermal-adaptive
  description: Reduce compute load when device overheats
spec:
  slot: vision

  rules:
    - name: thermal-throttle
      priority: 100
      conditions:
        all:
          - metric: platform.compute.cpu_temp_c
            operator: gte
            value: 75
            min_confidence: 0.7
      action:
        switch_to: yolov8-tiny
        preload:
          - mobilenet-fallback

    - name: critical-overheat
      priority: 200
      conditions:
        all:
          - metric: platform.compute.cpu_temp_c
            operator: gte
            value: 85
      action:
        switch_to: mobilenet-fallback

  fallback_chain:
    - yolov8-full
    - yolov8-tiny
    - mobilenet-fallback
```

### Battery Management

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: battery-adaptive
  description: Conserve power when battery is low
spec:
  slot: vision

  rules:
    - name: low-battery
      priority: 90
      conditions:
        all:
          - metric: platform.power.battery_pct
            operator: lte
            value: 20
          - metric: platform.power.power_source
            operator: eq
            value: battery
      action:
        switch_to: mobilenet-tiny

    - name: critical-battery
      priority: 200
      conditions:
        all:
          - metric: platform.power.battery_pct
            operator: lte
            value: 10
      action:
        switch_to: mobilenet-minimal

  fallback_chain:
    - yolov8-full
    - mobilenet-tiny
    - mobilenet-minimal
```

## Fallback Chains

When a model fails to load, TEMMS tries the fallback chain in order:

```yaml
fallback_chain:
  - yolov8-daylight      # Try this first
  - yolov8-lowlight      # Then this
  - mobilenet-tiny       # Last resort — should always work
```

The last model in the chain should be a minimal model that will always load, even under extreme resource pressure.

## Preloading

Preloading loads a model into memory without activating it. When a switch is needed, the preloaded model activates instantly (no disk I/O).

```yaml
action:
  switch_to: yolov8-lowlight
  preload:
    - mobilenet-tiny     # Keep this ready in case conditions worsen
```

Use preloading when you can predict what model might be needed next. The runtime keeps at most 3 preloaded models to limit memory usage.
