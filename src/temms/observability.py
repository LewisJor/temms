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


STATE_INDEX = ["PENDING", "DOWNLOADING", "READY", "FAILED", "OFFLINE", "DEGRADED"]


def set_deployment_state(state: str) -> None:
    for name in STATE_INDEX:
        deployment_state_gauge.labels(state=name).set(1 if name == state else 0)
