# SPDX-License-Identifier: MIT

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import kr8s
import structlog
from environs import Env
from pynixd.instance import Server, PynixdConfig
from pynixd.store import LocalSocketStore, SSHSocketStore, Store

# Configuration
env = Env()
env.read_env()

NAMESPACE = env.str("KUBE_NAMESPACE", "nixkube")
LABEL_SELECTOR = env.str("BUILDER_LABEL_SELECTOR", "app.kubernetes.io/component=builder")
SSH_USER = env.str("BUILDER_SSH_USER", "root")
SSH_KEY_PATH = env.path("BUILDER_SSH_KEY_PATH", "/etc/ssh-key/id_ed25519")
DISCOVERY_INTERVAL = env.int("DISCOVERY_INTERVAL_SECONDS", 30)

STATIC_BACKENDS_JSON = env.str("PYNIXD_STATIC_BACKENDS", "{}")

log = structlog.get_logger(__name__)

async def discover_builders(stores: dict[str, Store]):
    """Discover builder pods using kr8s and update the stores dictionary."""
    try:
        api = await kr8s.async_api()
        pods = await kr8s.async_get(
            "pods",
            namespace=NAMESPACE,
            label_selector=LABEL_SELECTOR,
            api=api,
        )

        current_discovered_names = set()
        for pod in pods:
            if pod.status.phase != "Running":
                continue
            
            pod_ip = pod.status.podIP
            if not pod_ip:
                continue
                
            pod_name = pod.metadata.name
            current_discovered_names.add(pod_name)
            
            if pod_name not in stores:
                log.info("builder_discovered", name=pod_name, ip=pod_ip)
                client_keys = [SSH_KEY_PATH] if SSH_KEY_PATH.exists() else None
                stores[pod_name] = SSHSocketStore(
                    host=pod_ip,
                    id=pod_name,
                    username=SSH_USER,
                    client_keys=client_keys,
                )
        
        # Remove pods that are no longer running or present
        # We only remove dynamic ones (not marked as static)
        to_remove = []
        for name, store in stores.items():
            if name not in current_discovered_names and not getattr(store, "is_static", False):
                to_remove.append(name)
                
        for name in to_remove:
            log.info("builder_removed", name=name)
            store = stores.pop(name)
            await store.close()

    except Exception:
        log.exception("discovery_failed")

async def discovery_loop(stores: dict[str, Store]):
    """Periodically run builder discovery."""
    while True:
        await asyncio.sleep(DISCOVERY_INTERVAL)
        await discover_builders(stores)

def load_static_backends(stores: dict[str, Store]):
    """Load static backends from environment variable."""
    try:
        backends = json.loads(STATIC_BACKENDS_JSON)
        for name, cfg in backends.items():
            # cfg example: {"host": "nixbuild.net", "user": "lillecarl", "type": "SSHSocketStore"}
            # Simplification: assume SSHSocketStore for now
            store = SSHSocketStore(
                host=cfg["host"],
                id=name,
                username=cfg.get("user"),
                port=cfg.get("port", 22),
                client_keys=cfg.get("keys"),
            )
            setattr(store, "is_static", True)
            stores[name] = store
            log.info("static_backend_loaded", name=name, host=cfg["host"])
    except Exception:
        log.exception("static_backends_load_failed")

async def main():
    # Configure structlog for JSON output if running in K8s
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ]
        )
    
    log.info("pynixd_nixkube_starting", namespace=NAMESPACE)
    
    local_store = LocalSocketStore(id="local", store_path=Path("/"))
    stores: dict[str, Store] = {}
    
    load_static_backends(stores)
    
    config = PynixdConfig(
        local_store=local_store,
        stores=stores,
        ssh_port=env.int("PYNIXD_SSH_PORT", 2222),
        http_port=env.int("PYNIXD_HTTP_PORT", 8080),
        unix_path=env.path("PYNIXD_UNIX_PATH", None),
    )
    
    server = Server(config)
    
    # Run initial discovery before starting server to have some builders ready
    await discover_builders(stores)
    
    discovery_task = asyncio.create_task(discovery_loop(stores))
    
    try:
        async with server:
            log.info("pynixd_nixkube_running")
            await server.wait_finished()
    finally:
        discovery_task.cancel()
        try:
            await discovery_task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
