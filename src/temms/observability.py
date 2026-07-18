from prometheus_client import Counter, Gauge, Histogram

inference_request_count = Counter("temms_inference_requests_total", "Inference requests served")
deployment_count = Counter("temms_deployments_total", "Deployment operations requested")
condition_update_count = Counter("temms_condition_updates_total", "Condition updates processed")
policy_decision_count = Counter("temms_policy_decisions_total", "Policy decisions evaluated")

inference_latency_ms = Histogram(
    "temms_inference_latency_ms",
    "Inference latency in milliseconds",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
)

deployment_state_gauge = Gauge(
    "temms_deployment_state",
    "Deployment lifecycle state",
    labelnames=("state",),
)
runtime_health_gauge = Gauge("temms_runtime_health", "Runtime health (1=healthy,0=unhealthy)")
uptime_gauge = Gauge("temms_uptime_seconds", "TEMMS uptime in seconds")

# ---- DDIL-specific metrics (issue #30) ----
# These make the differentiated behavior observable on a deployed edge, not just
# in simulation: swap activity, offline operation, the signed-intent queue, and
# the tamper-evident evidence chain.
model_swaps_total = Counter("temms_model_swaps_total", "Model activations (swaps) performed")
inference_errors_total = Counter("temms_inference_errors_total", "Inference requests that errored")
invalid_input_total = Counter(
    "temms_invalid_input_total",
    "Inference requests rejected as undecodable input (caller fault, not model failure)",
)
swap_latency_ms = Histogram(
    "temms_swap_latency_ms",
    "Model swap (load + warm + activate) latency in milliseconds",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)
offline_mode_gauge = Gauge("temms_offline_mode", "Edge in DDIL offline mode (1=offline,0=online)")
pending_intents_gauge = Gauge("temms_pending_intents", "DDIL signed-intent queue depth")
decision_chain_length_gauge = Gauge(
    "temms_decision_chain_length", "Length of the tamper-evident decision chain"
)
seconds_since_hub_sync_gauge = Gauge(
    "temms_seconds_since_hub_sync", "Seconds since the last successful Hub sync"
)


def set_ddil_gauges(
    *,
    offline: bool,
    pending_intents: int,
    decision_chain_length: int,
    seconds_since_sync: float | None,
) -> None:
    """Update the DDIL gauges from the daemon reconciliation loop."""
    offline_mode_gauge.set(1 if offline else 0)
    pending_intents_gauge.set(pending_intents)
    decision_chain_length_gauge.set(decision_chain_length)
    if seconds_since_sync is not None:
        seconds_since_hub_sync_gauge.set(seconds_since_sync)


STATE_INDEX = ["PENDING", "DOWNLOADING", "READY", "FAILED", "OFFLINE", "DEGRADED"]


def set_deployment_state(state: str) -> None:
    for name in STATE_INDEX:
        deployment_state_gauge.labels(state=name).set(1 if name == state else 0)
