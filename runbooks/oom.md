category: oom
# Runbook: containers OOMKilled

Signals: exit code 137, last state `OOMKilled`, memory sawtooth that climbs to the limit and drops.

1. Confirm the limit (`describe_pod`) and the peak working set (`query_metrics memory_usage`).
2. If a recent revision introduced the growth (new cache, new feature flag) and the previous revision ran
   well under the limit, **roll back** to the previous revision. This is the default for regressions.
3. If the workload legitimately needs more memory, **raise the memory limit** to about 1.5x the observed
   peak (round up to a power-of-two Mi value) and keep requests at roughly 75% of the limit.
4. If an env var sizes the cache or buffer, lowering it is an acceptable fix when the owning team agrees.
5. Never restart as the fix: a restart does not change the limit, so the loop comes back.
