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
_POLL_SECONDS = 10.0


class BuilderManager:
    def __init__(
        self,
        server: "Server",
        namespace: str,
        max_builders: int = 3,
        idle_timeout: int = 300,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        self.server = server
        self.namespace = namespace
        self.max_builders = max_builders
        self.idle_timeout = idle_timeout
        self.cooldown_seconds = cooldown_seconds
        self._last_create_time: float = 0.0
        self._registered: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        log.info(
            "builder_manager_started",
            namespace=self.namespace,
            max_builders=self.max_builders,
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
            self._poll_pods(),
            self._watch_queue(),
        )

    async def _poll_pods(self) -> None:
        import kr8s.asyncio as kr8s_asyncio

        while True:
            try:
                api = await kr8s_asyncio.api()
                async for pod in api.get(
                    "pods",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    await self._reconcile_pod(pod)
            except Exception as e:
                log.warning("builder_pod_poll_error", error=f"{type(e).__name__}: {e}")

            await asyncio.sleep(_POLL_SECONDS)

    async def _reconcile_pod(self, pod: object) -> None:
        raw = getattr(pod, "raw", {})
        pod_name = pod.metadata.name
        store_id = f"builder-{pod_name}"

        conditions = raw.get("status", {}).get("conditions", [])
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
        )
        container_statuses = raw.get("status", {}).get("containerStatuses", [])
        running = any(cs.get("ready", False) for cs in container_statuses)

        if ready and running and store_id not in self._registered:
            pod_ip = raw.get("status", {}).get("podIP")
            if pod_ip:
                await self._register_builder(store_id, pod_ip)
        elif (not running or not ready) and store_id in self._registered:
            await self._unregister_builder(store_id)

        phase = raw.get("status", {}).get("phase", "")
        if phase in ("Succeeded", "Failed") and store_id in self._registered:
            await self._unregister_builder(store_id)

    async def _register_builder(self, store_id: str, pod_ip: str) -> None:
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
            log.info("builder_registered", store_id=store_id, pod_ip=pod_ip)
        except Exception:
            log.exception("builder_register_failed", store_id=store_id, pod_ip=pod_ip)

    async def _unregister_builder(self, store_id: str) -> None:
        try:
            await self.server.remove_store(store_id)
            self._registered.pop(store_id, None)
            log.info("builder_unregistered", store_id=store_id)
        except Exception:
            log.exception("builder_unregister_failed", store_id=store_id)

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
