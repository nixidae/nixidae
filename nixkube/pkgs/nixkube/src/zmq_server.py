# SPDX-License-Identifier: MIT

"""ZeroMQ server for NRI build coordination."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import zmq.asyncio
from cachetools import TTLCache

logger = logging.getLogger("nixkube")


class ZeroMQServer:
    """Manages ZeroMQ PUB/REP sockets for build status coordination."""

    def __init__(self, socket_base_dir: str = "/nix/var/nix-csi"):
        """Initialize ZeroMQ server (sockets not yet created)."""
        self.socket_base_dir = socket_base_dir
        self.context: Optional[zmq.asyncio.Context] = None
        self.rep_socket: Optional[zmq.asyncio.Socket] = None
        self.pub_socket: Optional[zmq.asyncio.Socket] = None
        # Build status cache: container_id -> {"status": "done"|"pending", "timestamp": float}
        self.build_status: TTLCache = TTLCache(maxsize=10000, ttl=3600)
        self.pending_builds: set[str] = set()  # container IDs currently being built
        self.container_pids: dict[str, int] = {}  # container_id -> PID from nri-wait
        self.container_bundles: dict[
            str, str
        ] = {}  # container_id -> bundle from nri-wait
        self._pid_events: dict[
            str, asyncio.Event
        ] = {}  # signalled when PID+bundle arrive

    async def initialize(self) -> None:
        """Create ZeroMQ context and bind sockets."""
        logger.info(
            "[ZMQ-INIT] Initializing ZeroMQ sockets in %r", self.socket_base_dir
        )

        # Ensure socket directory exists
        socket_dir = Path(self.socket_base_dir)
        try:
            socket_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("[ZMQ-INIT] Socket directory ready: %s", socket_dir)
        except Exception as e:
            logger.error("[ZMQ-INIT] Failed to create socket directory: %s", e)
            raise

        # Create ZeroMQ context
        try:
            self.context = zmq.asyncio.Context()
            logger.debug("[ZMQ-INIT] ZeroMQ context created")
        except Exception as e:
            logger.error("[ZMQ-INIT] Failed to create ZMQ context: %s", e)
            raise

        # Create REP socket (for queries)
        try:
            req_socket_path = socket_dir / "wait-req.sock"
            self.rep_socket = self.context.socket(zmq.REP)
            self.rep_socket.bind(f"ipc://{req_socket_path}")
            logger.info("[ZMQ-INIT] REP socket bound to ipc://%s", req_socket_path)
        except Exception as e:
            logger.error("[ZMQ-INIT] Failed to create REP socket: %s", e)
            raise

        # Create PUB socket (for broadcasts)
        try:
            pub_socket_path = socket_dir / "wait-pub.sock"
            self.pub_socket = self.context.socket(zmq.PUB)
            self.pub_socket.bind(f"ipc://{pub_socket_path}")
            logger.info("[ZMQ-INIT] PUB socket bound to ipc://%s", pub_socket_path)
        except Exception as e:
            logger.error("[ZMQ-INIT] Failed to create PUB socket: %s", e)
            raise

        logger.info("[ZMQ-INIT] Both ZeroMQ sockets initialized successfully")

    def _pid_event(self, container_id: str) -> asyncio.Event:
        """Return (creating if needed) the Event for a container's PID arrival."""
        if container_id not in self._pid_events:
            self._pid_events[container_id] = asyncio.Event()
        return self._pid_events[container_id]

    async def wait_for_pid(
        self, container_id: str, timeout: float = 30.0
    ) -> tuple[int, str] | None:
        """Wait until nri-wait reports the container PID and bundle, then return (pid, bundle)."""
        try:
            await asyncio.wait_for(
                self._pid_event(container_id).wait(), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[ZMQ] Timed out waiting for PID of container=%r", container_id
            )
            return None
        pid = self.container_pids.get(container_id)
        bundle = self.container_bundles.get(container_id)
        if pid is None or bundle is None:
            logger.warning(
                "[ZMQ] Missing pid=%r or bundle=%r for container=%r",
                pid,
                bundle,
                container_id,
            )
            return None
        return (pid, bundle)

    async def query_build_status(self, container_id: str) -> dict:
        """Return build status for a container."""
        if container_id in self.build_status:
            return self.build_status[container_id]
        elif container_id in self.pending_builds:
            return {"status": "pending"}
        else:
            return {"status": "unknown"}

    async def publish_build_progress(self, container_id: str) -> None:
        """Publish build progress heartbeat on PUB socket to reset nri-wait timeout."""
        if self.pub_socket is None:
            logger.warning("PUB socket not initialized, cannot publish")
            return
        try:
            msg = json.dumps({"container_id": container_id, "status": "progress"})
            logger.debug("[ZMQ-PUB] Publishing progress: %s", msg)
            await self.pub_socket.send(msg.encode())
        except Exception as e:
            logger.warning(
                "[ZMQ-PUB] Failed to publish build progress for container=%r: %s",
                container_id,
                e,
            )

    async def publish_build_complete(self, container_id: str) -> None:
        """Publish build completion message on PUB socket."""
        if self.pub_socket is None:
            logger.warning("PUB socket not initialized, cannot publish")
            return
        try:
            msg = json.dumps({"container_id": container_id, "status": "done"})
            logger.debug("[ZMQ-PUB] Publishing: %s", msg)
            await self.pub_socket.send(msg.encode())
            logger.info(
                "[ZMQ-PUB] Published build completion for container=%r", container_id
            )
        except Exception as e:
            logger.error(
                "[ZMQ-PUB] Failed to publish build completion for container=%r: %s",
                container_id,
                e,
            )

    async def start_request_handler(self) -> None:
        """Handle build status queries on REP socket (blocks until cancelled)."""
        if self.rep_socket is None:
            logger.warning("REP socket not initialized, cannot handle requests")
            return

        logger.info("Starting ZeroMQ REP socket handler")
        try:
            while True:
                logger.debug("Waiting for build status query on REP socket...")
                query_bytes = await self.rep_socket.recv()
                logger.debug("Received query: %d bytes", len(query_bytes))
                try:
                    query = json.loads(query_bytes.decode())
                    container_id = query.get("id")
                    pid = query.get("pid")
                    bundle = query.get("bundle")
                    if (
                        pid is not None
                        and bundle is not None
                        and container_id is not None
                    ):
                        self.container_pids[container_id] = pid
                        self.container_bundles[container_id] = bundle
                        self._pid_event(container_id).set()
                        logger.debug(
                            "[ZMQ-REP] Stored pid=%d bundle=%r for container=%r",
                            pid,
                            bundle,
                            container_id,
                        )
                    logger.info(
                        "[ZMQ-REP] Query for container=%r pid=%r bundle=%r",
                        container_id,
                        pid,
                        bundle,
                    )

                    status = await self.query_build_status(container_id)
                    logger.debug(
                        "[ZMQ-REP] Responding with status=%s for container=%r",
                        status,
                        container_id,
                    )
                    response = json.dumps(status)
                    await self.rep_socket.send(response.encode())
                    logger.debug("[ZMQ-REP] Response sent")
                except Exception as e:
                    logger.error("Error handling query: %s", e)
                    await self.rep_socket.send(b'{"error":"internal error"}')
        except asyncio.CancelledError:
            logger.info("REP socket handler cancelled")
        except Exception as e:
            logger.error("REP socket handler error: %s", e)

    def shutdown(self) -> None:
        """Terminate ZeroMQ context."""
        if self.context is not None:
            self.context.term()
            logger.debug("[ZMQ-SHUTDOWN] ZeroMQ context terminated")
