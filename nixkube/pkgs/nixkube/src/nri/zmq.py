# SPDX-License-Identifier: MIT

"""ZeroMQ server for NRI build coordination."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import zmq.asyncio
from cachetools import TTLCache

logger = logging.getLogger("nixkube.nri.zmq")


@dataclass
class ContainerInfo:
    """Mutable container metadata collected during build coordination."""

    pid: int | None = None
    bundle: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


class ZeroMQServer:
    """Manages ZeroMQ PUB/REP sockets for build status coordination."""

    def __init__(self, socket_base_dir: str = "/nix/var/nixkube"):
        """Initialize ZeroMQ server (sockets not yet created)."""
        self.socket_base_dir = socket_base_dir
        self.context: zmq.asyncio.Context | None = None
        self.rep_socket: zmq.asyncio.Socket | None = None
        self.pub_socket: zmq.asyncio.Socket | None = None
        self.build_status: TTLCache[str, dict[str, str]] = TTLCache(
            maxsize=10000, ttl=3600
        )
        self.pending_builds: set[str] = set()
        # Container metadata from nri-wait, TTL-evicted to avoid unbounded growth
        self._container_info: TTLCache[str, ContainerInfo] = TTLCache(
            maxsize=10000, ttl=3600
        )

    async def initialize(self) -> None:
        """Create ZeroMQ context and bind sockets."""
        logger.info(f"Initializing ZeroMQ sockets in {self.socket_base_dir!r}")

        socket_dir = Path(self.socket_base_dir)
        try:
            socket_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Socket directory ready: {socket_dir}")
        except Exception as e:
            logger.error(f"Failed to create socket directory: {e}")
            raise

        try:
            self.context = zmq.asyncio.Context()
            logger.debug("ZeroMQ context created")
        except Exception as e:
            logger.error(f"Failed to create ZMQ context: {e}")
            raise

        try:
            req_socket_path = socket_dir / "wait-req.sock"
            self.rep_socket = self.context.socket(zmq.REP)
            self.rep_socket.bind(f"ipc://{req_socket_path}")
            logger.info(f"REP socket bound to ipc://{req_socket_path}")

            pub_socket_path = socket_dir / "wait-pub.sock"
            self.pub_socket = self.context.socket(zmq.PUB)
            self.pub_socket.bind(f"ipc://{pub_socket_path}")
            logger.info(f"PUB socket bound to ipc://{pub_socket_path}")
        except Exception as e:
            logger.error(f"Failed to create ZeroMQ sockets: {e}")
            self.shutdown()
            raise

        logger.info("Both ZeroMQ sockets initialized successfully")

    def _get_info(self, container_id: str) -> ContainerInfo:
        """Return (creating if needed) the ContainerInfo for a container."""
        if container_id not in self._container_info:
            self._container_info[container_id] = ContainerInfo()
        return self._container_info[container_id]

    async def wait_for_pid(
        self, container_id: str, timeout: float = 30.0
    ) -> tuple[int, str] | None:
        """Wait until nri-wait reports the container PID and bundle, then return (pid, bundle)."""
        info = self._get_info(container_id)
        try:
            await asyncio.wait_for(info.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Timed out waiting for PID of container={container_id!r}")
            return None
        if info.pid is None or info.bundle is None:
            logger.warning(
                f"Missing pid={info.pid!r} or bundle={info.bundle!r} for container={container_id!r}"
            )
            return None
        return (info.pid, info.bundle)

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
            logger.debug(f"Publishing progress: {msg}")
            await self.pub_socket.send(msg.encode())
        except Exception as e:
            logger.warning(
                f"Failed to publish build progress for container={container_id!r}: {e}"
            )

    async def publish_build_complete(self, container_id: str) -> None:
        """Publish build completion message on PUB socket."""
        if self.pub_socket is None:
            logger.warning("PUB socket not initialized, cannot publish")
            return
        try:
            msg = json.dumps({"container_id": container_id, "status": "done"})
            logger.debug(f"Publishing: {msg}")
            await self.pub_socket.send(msg.encode())
            logger.info(f"Published build completion for container={container_id!r}")
        except Exception as e:
            logger.error(
                f"Failed to publish build completion for container={container_id!r}: {e}"
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
                logger.debug(f"Received query: {len(query_bytes)} bytes")
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
                        info = self._get_info(container_id)
                        info.pid = pid
                        info.bundle = bundle
                        info.event.set()
                        logger.debug(
                            f"Stored pid={pid} bundle={bundle!r} for container={container_id!r}"
                        )
                    logger.info(
                        f"Query for container={container_id!r} pid={pid!r} bundle={bundle!r}"
                    )

                    status = await self.query_build_status(container_id)
                    logger.debug(
                        f"Responding with status={status} for container={container_id!r}"
                    )
                    response = json.dumps(status)
                    await self.rep_socket.send(response.encode())
                    logger.debug("Response sent")
                except Exception as e:
                    logger.error(f"Error handling query: {e}")
                    await self.rep_socket.send(b'{"error":"internal error"}')
        except asyncio.CancelledError:
            logger.info("REP socket handler cancelled")
        except Exception as e:
            logger.error(f"REP socket handler error: {e}")

    def shutdown(self) -> None:
        """Terminate ZeroMQ context."""
        if self.context is not None:
            self.context.term()
            logger.debug("ZeroMQ context terminated")
