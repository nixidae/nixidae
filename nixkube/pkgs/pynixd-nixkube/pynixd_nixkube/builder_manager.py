# SPDX-License-Identifier: MIT

import asyncio
import contextlib
import time
import uuid
from typing import TYPE_CHECKING

import structlog
from kr8s.asyncio.objects import Job, PodTemplate
from pynixd.store import SSHSubprocessStore

if TYPE_CHECKING:
    from pynixd.instance import Server

log = structlog.get_logger(__name__)

BUILDER_LABEL = "app.kubernetes.io/component"
BUILDER_LABEL_VALUE = "builder"
BUILDER_PODTEMPLATE_NAME = "nixkube-builder"
SYSTEM_LABEL = "nixkube/system"

KUBE_ARCH_TO_NIX_SYSTEM = {
    "amd64": "x86_64-linux",
    "arm64": "aarch64-linux",
}
NIX_SYSTEM_TO_KUBE_ARCH = {v: k for k, v in KUBE_ARCH_TO_NIX_SYSTEM.items()}

_DEFAULT_FEATURES = {"nixos-test", "big-parallel", "benchmark"}

_COOLDOWN_SECONDS = 60.0
_RECONNECT_DELAY = 10.0


class BuilderManager:
    def __init__(
        self,
        server: "Server",
        namespace: str,
        max_builders: int = 3,
        min_builders: int = 1,
        idle_timeout: int = 300,
        systems: list[str] | None = None,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        self.server = server
        self.namespace = namespace
        self.max_builders = max_builders
        self.min_builders = min_builders
        self.idle_timeout = idle_timeout
        self.systems = systems or ["x86_64-linux"]
        self.cooldown_seconds = cooldown_seconds
        self._last_create_time: dict[str, float] = {}
        self._registered: dict[str, str] = {}
        self._idle_since: dict[str, float] = {}
        self._job_names: dict[str, str] = {}
        self._available_systems: set[str] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._sync_node_features()
        await self._reap_orphaned_builder_pods()
        self._task = asyncio.create_task(self._run())
        log.info(
            "builder_manager_started",
            namespace=self.namespace,
            max_builders=self.max_builders,
            min_builders=self.min_builders,
            systems=list(self._available_systems),
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("builder_manager_stopped")

    async def _run(self) -> None:
        await asyncio.gather(
            self._watch_jobs(),
            self._watch_queue(),
            self._reap_idle(),
            self._reconcile_node_features(),
        )

    # ---- Node discovery and dynamic feature management ----

    async def _sync_dynamic_features(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return
        current = set(scheduler.dynamic_feature_matrix)
        added = self._available_systems - current
        removed = current - self._available_systems
        for system in removed:
            scheduler._dynamic_feature_matrix.pop(system, None)
            log.info("dynamic_feature_removed", system=system)
        if added:
            scheduler.add_dynamic_features(
                {sys: set(_DEFAULT_FEATURES) for sys in added}
            )
            log.info("dynamic_features_added", systems=sorted(added))

    async def _probe_node(self, node: object) -> str | None:
        labels = getattr(node.metadata, "labels", {}) or {}
        existing = labels.get(SYSTEM_LABEL)
        if existing:
            return existing

        kube_arch = labels.get("kubernetes.io/arch")
        if not kube_arch:
            return None

        nix_system = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
        if not nix_system:
            return None

        try:
            await node.patch(
                {"metadata": {"labels": {SYSTEM_LABEL: nix_system}}},
                type="merge",
            )
            log.info("node_probed", node=node.metadata.name, system=nix_system)
        except Exception:
            log.warning("node_label_failed", node=node.metadata.name, system=nix_system)

        return nix_system

    async def _sync_node_features(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        enabled = set(self.systems)
        available: set[str] = set()

        try:
            api = await kr8s_asyncio.api()
            async for node in api.get("nodes"):
                system = await self._probe_node(node)
                if system and system in enabled:
                    available.add(system)
        except Exception:
            log.exception("node_discovery_error")
            return

        old = self._available_systems
        self._available_systems = available

        await self._sync_dynamic_features()

        if available != old:
            log.info(
                "available_systems_changed",
                available=sorted(available),
                removed=sorted(old - available),
                added=sorted(available - old),
            )

    async def _reconcile_node_features(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self._sync_node_features()
            except Exception:
                log.exception("node_feature_reconcile_error")

    # ---- Job watching and reconciliation ----

    async def _watch_jobs(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        while True:
            try:
                api = await kr8s_asyncio.api()

                seen: set[str] = set()
                async for job in api.get(
                    "jobs",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    seen.add(job.metadata.name)
                    await self._reconcile_job(job)

                for store_id in list(self._registered):
                    store_id_prefix = "builder-"
                    if store_id.startswith(store_id_prefix):
                        job_name = store_id[len(store_id_prefix) :]
                        if job_name not in seen:
                            await self._unregister_builder(store_id)

                await self._ensure_min_builders()

                async for event in api.watch(
                    "jobs",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    if isinstance(event, tuple):
                        event_type, job = event
                    else:
                        event_type = event.get("type")
                        job = event.get("object")
                    if job is None:
                        continue

                    if event_type == "DELETED":
                        store_id = f"builder-{job.metadata.name}"
                        if store_id in self._registered:
                            await self._unregister_builder(store_id)
                        await self._ensure_min_builders()
                    elif event_type in ("ADDED", "MODIFIED"):
                        await self._reconcile_job(job)
                        await self._ensure_min_builders()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("builder_watch_error", error=f"{type(e).__name__}: {e}")

            await asyncio.sleep(_RECONNECT_DELAY)

    async def _reconcile_job(self, job: object) -> None:
        raw = getattr(job, "raw", {})
        job_name = job.metadata.name
        store_id = f"builder-{job_name}"

        if await self._is_stale_builder(job):
            log.info("builder_stale", store_id=store_id, job_name=job_name)
            await self._unregister_builder(store_id)
            await self._delete_job(job)
            return

        status = raw.get("status", {})
        if status.get("succeeded") or status.get("failed"):
            if store_id in self._registered:
                await self._unregister_builder(store_id)
            return

        if store_id not in self._registered:
            pod_ip = await self._get_job_pod_ip(job)
            if pod_ip:
                await self._register_builder(store_id, pod_ip, job_name)
        else:
            self._job_names[store_id] = job_name

    async def _register_builder(
        self, store_id: str, pod_ip: str, job_name: str = ""
    ) -> None:
        store = SSHSubprocessStore(
            host=pod_ip,
            store_id=store_id,
            port=22,
            username="nix",
            client_keys=["/etc/ssh-key/id_ed25519"],
            nix_bin="/nix/var/result/bin/nix",
            monitor=False,
        )
        try:
            await self.server.add_store(store, dynamic=True)
            self._registered[store_id] = pod_ip
            self._idle_since[store_id] = time.monotonic()
            if job_name:
                self._job_names[store_id] = job_name
            log.info("builder_registered", store_id=store_id, pod_ip=pod_ip)
        except Exception:
            log.exception("builder_register_failed", store_id=store_id, pod_ip=pod_ip)

    async def _unregister_builder(self, store_id: str) -> None:
        try:
            await self.server.remove_store(store_id)
            self._registered.pop(store_id, None)
            self._idle_since.pop(store_id, None)
            self._job_names.pop(store_id, None)
            log.info("builder_unregistered", store_id=store_id)
        except Exception:
            log.exception("builder_unregister_failed", store_id=store_id)

    async def _is_stale_builder(self, job: object) -> bool:
        expected = await self._get_expected_store_versions()
        if not expected:
            return False
        job_versions = self._get_job_store_versions(job)
        return not job_versions <= expected

    @staticmethod
    def _get_job_store_versions(job: object) -> set[str]:
        for vol in (
            getattr(job, "raw", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("volumes", [])
        ):
            csi = vol.get("csi", {})
            if csi.get("driver") == "nixkube" and csi.get("readOnly") is False:
                found = set()
                for val in csi.get("volumeAttributes", {}).values():
                    if isinstance(val, str) and val.startswith("/nix/store/"):
                        found.add(val)
                return found
        return set()

    async def _get_expected_store_versions(self) -> set[str]:
        try:
            template = await PodTemplate.get(
                BUILDER_PODTEMPLATE_NAME, namespace=self.namespace
            )
        except Exception:
            log.exception("builder_podtemplate_get_failed")
            return set()
        pod_spec = template.raw.get("template", {})
        for vol in pod_spec.get("spec", {}).get("volumes", []):
            csi = vol.get("csi", {})
            if csi.get("driver") == "nixkube" and csi.get("readOnly") is False:
                found = set()
                for val in csi.get("volumeAttributes", {}).values():
                    if isinstance(val, str) and val.startswith("/nix/store/"):
                        found.add(val)
                return found
        return set()

    async def _get_job_pod_ip(self, job: object) -> str | None:
        import kr8s.asyncio as kr8s_asyncio

        try:
            api = await kr8s_asyncio.api()
            async for pod in api.get(
                "pods",
                namespace=self.namespace,
                label_selector={"job-name": job.metadata.name},
            ):
                pod_ip = pod.raw.get("status", {}).get("podIP")
                if pod_ip:
                    return pod_ip
        except Exception:
            log.exception("job_pod_ip_fetch_failed", job=job.metadata.name)
        return None

    # ---- Builder min/max lifecycle ----

    async def _ensure_min_builders(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        try:
            api = await kr8s_asyncio.api()
            active_by_system: dict[str, int] = {}
            async for job in api.get(
                "jobs",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                status = getattr(job, "raw", {}).get("status", {})
                if not status.get("succeeded") and not status.get("failed"):
                    labels = getattr(job.metadata, "labels", {}) or {}
                    sys_label = labels.get(SYSTEM_LABEL, "unknown")
                    active_by_system[sys_label] = active_by_system.get(sys_label, 0) + 1
        except Exception:
            log.exception("builder_job_count_error")
            return

        total_active = sum(active_by_system.values())

        for system in sorted(self._available_systems):
            active = active_by_system.get(system, 0)
            if active >= self.min_builders:
                continue
            if total_active >= self.max_builders:
                break

            log.info(
                "ensuring_min_builders",
                system=system,
                active=active,
                min_builders=self.min_builders,
                max_builders=self.max_builders,
            )
            await self._create_builder_job(system=system)
            total_active += 1
            await asyncio.sleep(0.5)

    async def _maybe_create_builder(self, system: str) -> None:
        now = time.monotonic()
        last = self._last_create_time.get(system, 0.0)
        if now - last < self.cooldown_seconds:
            return

        import kr8s.asyncio as kr8s_asyncio

        try:
            api = await kr8s_asyncio.api()
            active = 0
            async for job in api.get(
                "jobs",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                status = getattr(job, "raw", {}).get("status", {})
                if not status.get("succeeded") and not status.get("failed"):
                    active += 1
        except Exception:
            log.exception("builder_job_count_error")
            return

        if active >= self.max_builders:
            log.debug(
                "builder_max_reached",
                active=active,
                max=self.max_builders,
                system=system,
            )
            return

        log.info(
            "creating_builder_job", active=active, max=self.max_builders, system=system
        )
        await self._create_builder_job(system=system)
        self._last_create_time[system] = now

    async def _reap_idle(self) -> None:
        while True:
            try:
                total = len(self._registered)
                if total <= self.min_builders:
                    await asyncio.sleep(60)
                    continue

                now = time.monotonic()
                for store_id, pod_ip in list(self._registered.items()):
                    store = self.server.stores.get(store_id)
                    if store is None:
                        continue

                    if store.in_flight > 0:
                        self._idle_since[store_id] = now
                        continue

                    idle_seconds = now - self._idle_since.get(store_id, now)
                    if idle_seconds < self.idle_timeout:
                        continue

                    if len(self._registered) <= self.min_builders:
                        break

                    log.info(
                        "builder_idle_timeout",
                        store_id=store_id,
                        pod_ip=pod_ip,
                        idle_seconds=idle_seconds,
                    )
                    await self._delete_builder_job(store_id)
            except Exception:
                log.exception("builder_idle_reap_error")

            await asyncio.sleep(60)

    async def _delete_job(self, job: object) -> None:
        try:
            await job.delete(propagation_policy="Background")
            log.info("builder_job_deleted", job=job.metadata.name)
        except Exception:
            log.exception("builder_job_delete_failed", job=job.metadata.name)

    async def _delete_builder_job(self, store_id: str) -> None:
        job_name = self._job_names.get(store_id)
        if not job_name:
            return

        try:
            job = await Job.get(job_name, namespace=self.namespace)
            await self._delete_job(job)
        except Exception:
            log.exception("builder_job_delete_idle_failed", job=job_name)

    async def _reap_orphaned_builder_pods(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        try:
            api = await kr8s_asyncio.api()
            async for pod in api.get(
                "pods",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                refs = getattr(pod.metadata, "ownerReferences", None)
                if not refs:
                    log.info("orphaned_builder_pod_found", pod=pod.metadata.name)
                    await pod.delete(propagation_policy="Background")
        except Exception:
            log.exception("orphaned_builder_pod_cleanup_error")

    # ---- Queue watching for reactive builder creation ----

    async def _watch_queue(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return

        while True:
            try:
                needed_systems: set[str] = set()
                for build in scheduler.queue.queue:
                    if build.is_pending and build.platform in self._available_systems:
                        needed_systems.add(build.platform)

                if not needed_systems:
                    await asyncio.sleep(5)
                    continue

                active_per_system: dict[str, int] = {}
                for s in self.server.stores.values():
                    if (
                        s.store_id.startswith("builder-")
                        and s.is_healthy
                        and not s.draining
                    ):
                        fm = s.feature_matrix
                        if fm:
                            for system in fm:
                                active_per_system[system] = (
                                    active_per_system.get(system, 0) + 1
                                )
                        else:
                            for system in self._available_systems:
                                active_per_system[system] = (
                                    active_per_system.get(system, 0) + 1
                                )

                for system in sorted(needed_systems):
                    if active_per_system.get(system, 0) == 0:
                        await self._maybe_create_builder(system)
            except Exception:
                log.exception("builder_queue_watch_error")

            await asyncio.sleep(5)

    # ---- Job creation ----

    async def _create_builder_job(self, system: str) -> None:
        try:
            template = await PodTemplate.get(
                BUILDER_PODTEMPLATE_NAME, namespace=self.namespace
            )
        except Exception:
            log.error("builder_podtemplate_not_found", name=BUILDER_PODTEMPLATE_NAME)
            return

        pod_spec = template.raw.get("template", {})

        kube_arch = NIX_SYSTEM_TO_KUBE_ARCH.get(system)
        if kube_arch:
            spec = pod_spec.setdefault("spec", {})
            node_selector = spec.setdefault("nodeSelector", {})
            node_selector["kubernetes.io/arch"] = kube_arch

        job_id = str(uuid.uuid4())[:8]
        job_name = f"nixkube-builder-{job_id}"

        job_resource = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace,
                "labels": {
                    BUILDER_LABEL: BUILDER_LABEL_VALUE,
                    SYSTEM_LABEL: system,
                },
            },
            "spec": {
                "ttlSecondsAfterFinished": 300,
                "backoffLimit": 0,
                "template": pod_spec,
            },
        }

        try:
            job = await Job(job_resource)
            await job.create()
            log.info("builder_job_created", job=job_name, system=system)
        except Exception as e:
            log.exception(
                "builder_job_create_failed",
                job=job_name,
                system=system,
                error=str(e),
            )
