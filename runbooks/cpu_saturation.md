category: cpu_saturation
# Runbook: CPU saturation / throttling

Signals: CPU usage pinned at the container CPU limit, rising p99 latency, timeouts in callers.

1. Identify the saturated workload. It is often a downstream dependency, not the alerting service.
2. Prefer **scaling out** (scale_deployment) when traffic grew: add replicas so per-pod CPU drops below 70%.
3. Raise the CPU limit only when a single request is CPU-heavy and scaling out would not help.
4. Check callers recover (5xx rate and p99 latency) after the change.
