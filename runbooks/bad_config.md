category: bad_config
# Runbook: bad configuration value

Signals: CrashLoopBackOff with exit code 1 right after start, config or parse errors in previous logs,
a recent revision that changed environment variables.

1. Read the previous container's logs to find the exact variable and the bad value.
2. Either **set the variable to the correct value** (set_env) when the right value is clear from logs or
   the previous revision, or **roll back** to the last good revision.
3. Do not raise resources or restart: the config is the problem.
