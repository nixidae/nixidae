# Pynixd-Nixkube Improvements

Improvements for the interaction between pynixd, nixkube and Kubernetes, discussed
[2026-05-25](./../../.opencode/logs/2026-05-25.md).

- ✅ Agreed + actionable
- 👀 Agreed but lower priority
- ❌ Not a priority (accepted tradeoff or non-issue)

## Builder Manager Resiliency (✅)

Fix race conditions and edge cases in the 726-line autoscaling builder pool:

- **Creation race**: `_ensure_min_builders` and `_maybe_create_builder` both list
  jobs independently then create. Two concurrent calls can over-provision. Fix
  with a creation lock or make the watcher the sole creator.
- **Orphan reaping**: `_reap_orphaned_builder_pods` runs once at startup only.
  Orphaned pods created later are never cleaned. Run it periodically.
- **Probe timeout**: `_pending_probes` has no timeout — a hanging probe blocks
  reprobing of that node forever. Add a timeout + retry.
- **Rate limiting**: `_ensure_min_builders` bypasses cooldown. Rapid Deployment
  scale-up events can burst Job creations. Respect cooldown in all creation
  paths.
- **Lost registrations on restart**: In-flight `SSHSubprocessStore` connections
  are lost when the cache pod restarts. Persist store registrations to SQLite
  (pynixd already has `use_db`), then re-register on startup and let pynixd
  reschedule failed builds. (Note: losing individual in-flight builds when the
  cache itself dies is acceptable — this is about recovering the pool, not
  preserving individual builds.)

## Observability (✅)

- **Prometheus metrics**: Queue depth, builder utilization, cache hit ratio, CSI
  operation latency. pynixd already has `PrometheusMetrics` — wire it up.
- **Health endpoint**: Add an HTTP `/healthz` that returns 200 only after
  `server` + `builder_manager` are initialized (stores registered, reconciler
  running). The current SSH TCP probe passes even before initialization.
- **Structured builder lifecycle**: Add more structured fields (system,
  store_id, pod_name) and correlation IDs across builder lifecycle events.

## GC Owned by Pynixd (✅)

Replace the bash-based GC script with pynixd-managed GC:

- **Current state**: `environments/cache/default.nix` has a dinit-managed GC
  service that runs `nix store delete` via bash + `shuf` for jitter. It has no
  coordination with pynixd — it could delete paths being served.
- **Target state**: pynixd already has `StoreMonitor` and `use_db` for tracking
  path metadata. Have pynixd periodically delete paths with zero references or
  last-access beyond retention. This removes the bash GC entirely.
- **Bonus**: The `IS_CACHE=true` env var (needed for store signing in the GC
  script) is never set in the Kubernetes manifests, so store signing never
  actually happens. Either wire it up or remove the dead code.

## Resource-Aware Builder Scheduling (👀)

Builders are created purely by `system` availability. No consideration of:

- Current CPU/memory utilization of existing builders
- Node resource pressure
- Build queue size vs. builder count correlation

Fix: expose builder resource metrics (via Prometheus), and let the builder
manager consider utilization in scaling decisions. Or use the K8s scheduler's
node selection (`_schedule_builder` picks nodes with available capacity).

## Replace Environments (👀)

The `environments/` directory (dinix-based service sets with bash GC scripts)
should eventually be replaced by something cleaner. Store signing via the bash
script is dead code (never actually runs).

## Not A Priority

These were discussed but accepted as tradeoffs or non-issues for now:

- **Bootstrap ordering**: The cache's CSI `init-store` volume creates a circular
  dependency on fresh clusters. Dismissed because the cache can fetch from
  cachix instead.
- **Secrets management**: The `init-secrets` kluctl hook regenerates SSH keys on
  every deploy (destructive). Current behavior is intentional — it only runs
  when something is missing, and destroying + recreating is acceptable.
- **In-flight build recovery on cache crash**: Losing in-flight builds when the
  cache pod restarts is an acceptable tradeoff.
- **Builder security**: Privileged builder pods are an accepted tradeoff —
  pynixd-nixkube should not be used to run untrusted builds.
