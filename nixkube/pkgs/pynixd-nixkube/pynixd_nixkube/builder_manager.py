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
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        self.server = server
        self.namespace = namespace
        self.max_builders = max_builders
        self.min_builders = min_builders
        self.idle_timeout = idle_timeout
        self.cooldown_seconds = cooldown_seconds
        self._last_create_time: float = 0.0
        self._registered: dict[str, str] = {}
        self._idle_since: dict[str, float] = {}
        self._job_names: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        log.info(
            "builder_manager_started",
            namespace=self.namespace,
            max_builders=self.max_builders,
            min_builders=self.min_builders,
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
            self._watch_pods(),
            self._watch_queue(),
            self._reap_idle(),
        )

    async def _watch_pods(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        while True:
            try:
                api = await kr8s_asyncio.api()

                seen: set[str] = set()
                async for pod in api.get(
                    "pods",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    pod_name = pod.metadata.name
                    seen.add(pod_name)
                    await self._reconcile_pod(pod)

                for store_id in list(self._registered):
                    store_id_prefix = "builder-"
                    if store_id.startswith(store_id_prefix):
                        pod_name = store_id[len(store_id_prefix):]
                        if pod_name not in seen:
                            await self._unregister_builder(store_id)

                await self._ensure_min_builders()

                async for event in api.watch(
                    "pods",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    event_type = event.get("type")
                    pod = event.get("object")
                    if pod is None:
                        continue

                    if event_type == "DELETED":
                        store_id = f"builder-{pod.metadata.name}"
                        if store_id in self._registered:
                            await self._unregister_builder(store_id)
                        await self._ensure_min_builders()
                    elif event_type in ("ADDED", "MODIFIED"):
                        await self._reconcile_pod(pod)
                        await self._ensure_min_builders()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("builder_watch_error", error=f"{type(e).__name__}: {e}")

            await asyncio.sleep(_RECONNECT_DELAY)

    async def _reconcile_pod(self, pod: object) -> None:
        raw = getattr(pod, "raw", {})
        pod_name = pod.metadata.name
        store_id = f"builder-{pod_name}"
        job_name = raw.get("metadata", {}).get("labels", {}).get("job-name", pod_name)

        conditions = raw.get("status", {}).get("conditions", [])
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
        )
        container_statuses = raw.get("status", {}).get("containerStatuses", [])
        running = any(cs.get("ready", False) for cs in container_statuses)

        if ready and running and store_id not in self._registered:
            pod_ip = raw.get("status", {}).get("podIP")
            if pod_ip:
                await self._register_builder(store_id, pod_ip, job_name)
        elif (not running or not ready) and store_id in self._registered:
            await self._unregister_builder(store_id)
        elif ready and running:
            self._job_names[store_id] = job_name

        phase = raw.get("status", {}).get("phase", "")
        if phase in ("Succeeded", "Failed") and store_id in self._registered:
            await self._unregister_builder(store_id)

    async def _register_builder(self, store_id: str, pod_ip: str, job_name: str = "") -> None:
        store = SSHSubprocessStore(
            host=pod_ip,
            store_id=store_id,
            port=2222,
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

    async def _ensure_min_builders(self) -> None:
        active = await self._count_active_jobs()
        if active >= self.min_builders:
            return

        needed = min(self.min_builders - active, self.max_builders - active)
        if needed <= 0:
            return

        log.info(
            "ensuring_min_builders",
            active=active,
            min_builders=self.min_builders,
            max_builders=self.max_builders,
            creating=needed,
        )
        for _ in range(needed):
            await self._create_builder_job()
            await asyncio.sleep(0.5)

    async def _reap_idle(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

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

    async def _delete_builder_job(self, store_id: str) -> None:
        job_name = self._job_names.get(store_id)
        if not job_name:
            return

        import kr8s.asyncio as kr8s_asyncio
        try:
            api = await kr8s_asyncio.api()
            job = await Job.get(job_name, namespace=self.namespace, api=api)
            await job.delete()
            log.info("builder_job_deleted_idle", job=job_name, store_id=store_id)
        except Exception:
            log.exception("builder_job_delete_idle_failed", job=job_name)

    async def _watch_queue(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return

        while True:
            try:
                pending = scheduler.queue.count(status="pending")
                if pending > 0:
                    available = sum(
                        1
                        for s in self.server.stores.values()
                        if s.is_healthy and not s.draining and s.in_flight < 4
                    )
                    if available == 0:
                        await self._maybe_create_builder()
            except Exception:
                log.exception("builder_queue_watch_error")

            await asyncio.sleep(5)

    async def _maybe_create_builder(self) -> None:
        now = time.monotonic()
        if now - self._last_create_time < self.cooldown_seconds:
            return

        active_jobs = await self._count_active_jobs()
        if active_jobs >= self.max_builders:
            log.debug("builder_max_reached", active=active_jobs, max=self.max_builders)
            return

        log.info("creating_builder_job", active=active_jobs, max=self.max_builders)
        await self._create_builder_job()
        self._last_create_time = now

    async def _count_active_jobs(self) -> int:
        import kr8s.asyncio as kr8s_asyncio

        try:
            api = await kr8s_asyncio.api()
            count = 0
            async for job in api.get(
                "jobs",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                status = getattr(job, "raw", {}).get("status", {})
                if not status.get("succeeded") and not status.get("failed"):
                    count += 1
            return count
        except Exception:
            log.exception("builder_job_count_error")
            return self.max_builders

    async def _create_builder_job(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        template = await self._get_pod_template()
        if not template:
            log.error("builder_podtemplate_not_found", name=BUILDER_PODTEMPLATE_NAME)
            return

        pod_spec = template.raw.get("template", {})

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
                },
            },
            "spec": {
                "ttlSecondsAfterFinished": 300,
                "backoffLimit": 0,
                "template": pod_spec,
            },
        }

        api = await kr8s_asyncio.api()
        job = Job(job_resource, api=api)
        try:
            await job.create()
            log.info("builder_job_created", job=job_name)
        except Exception as e:
            log.exception("builder_job_create_failed", job=job_name, error=str(e))

    async def _get_pod_template(self) -> PodTemplate | None:
        try:
            return await PodTemplate.get(
                BUILDER_PODTEMPLATE_NAME,
                namespace=self.namespace,
            )
        except Exception:
            log.exception("builder_podtemplate_get_failed")
            return None
