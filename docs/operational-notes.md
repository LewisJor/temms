# Operational Notes

## Edge runtime services
- TEMMS daemon/API on :8080
- Prometheus on :9090
- Grafana on :3000
- Optional MLflow on :5000

## Key metrics
- request count, latency histogram
- deployment count/state
- runtime health
- condition updates
- policy decisions
- uptime

Use `/metrics` for scraping.
