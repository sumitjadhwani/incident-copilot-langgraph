category: dependency_down
# Runbook: dependency unavailable

Signals: connection refused / timeouts to a database or service in the logs of healthy-looking pods.

1. Find the dependency's deployment and check its replicas and pod status.
2. If it was scaled to zero (check rollout history and events), **scale it back** to its previous replica count.
3. If it is crash looping, diagnose that workload instead; do not restart callers.
4. Callers usually recover on their own once the dependency is back.
