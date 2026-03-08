# SPDX-License-Identifier: MIT

"""ZeroMQ server for NRI build coordination.

Why ZeroMQ instead of raw asyncio streams or queues?

1. **Cross-namespace IPC**: nri-wait runs inside the container's network namespace
   after the OCI hook fires. Unix domain sockets work across Linux namespaces at the
   filesystem level, and ZeroMQ's IPC transport uses exactly that. Replicating this
   cleanly with asyncio streams would require the same Unix socket plumbing but without
   ZeroMQ's framing, reconnection, and buffering guarantees.

2. **PUB/SUB heartbeat pump**: The build daemon publishes periodic progress messages
   so nri-wait can distinguish "build still running" from "daemon crashed". ZeroMQ's
   PUB/SUB gives us fan-out (multiple nri-wait processes can subscribe) and late-join
   semantics for free. Reimplementing this with asyncio would mean maintaining a set of
   writer streams and handling partial failures manually.

3. **Battle-tested**: pyzmq is a mature, well-maintained binding to the ZeroMQ C
   library. The socket lifecycle (bind/connect, context cleanup) is well-understood and
   the async support integrates cleanly with asyncio event loops.

Design:
- REP socket (`wait-req.sock`): nri-wait sends its PID/bundle and polls for build status.
- PUB socket (`wait-pub.sock`): build daemon broadcasts progress/done events; nri-wait
  subscribes and resets its timeout on each heartbeat.
"""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import zmq.asyncio
from cachetools import TTLCache

logger = structlog.get_logger("nixkube.nri.zmq")


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
        logger.info("zmq_initializing", socket_base_dir=self.socket_base_dir)

        socket_dir = Path(self.socket_base_dir)
        try:
            socket_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("zmq_socket_dir_ready", socket_dir=str(socket_dir))
        except Exception:
            logger.exception("zmq_socket_dir_failed")
            raise

        try:
            self.context = zmq.asyncio.Context()
            logger.debug("zmq_context_created")
        except Exception:
            logger.exception("zmq_context_failed")
            raise

        try:
            req_socket_path = socket_dir / "wait-req.sock"
            self.rep_socket = self.context.socket(zmq.REP)
            self.rep_socket.bind(f"ipc://{req_socket_path}")
            logger.info("zmq_rep_bound", path=str(req_socket_path))

            pub_socket_path = socket_dir / "wait-pub.sock"
            self.pub_socket = self.context.socket(zmq.PUB)
            self.pub_socket.bind(f"ipc://{pub_socket_path}")
            logger.info("zmq_pub_bound", path=str(pub_socket_path))
        except Exception:
            logger.exception("zmq_sockets_failed")
            self.shutdown()
            raise

        logger.info("zmq_initialized")

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
            logger.warning("zmq_pid_timeout", container_id=container_id)
            return None
        if info.pid is None or info.bundle is None:
            logger.warning(
                "zmq_pid_missing",
                container_id=container_id,
                pid=info.pid,
                bundle=info.bundle,
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
            logger.warning("zmq_pub_not_initialized")
            return
        try:
            msg = json.dumps({"container_id": container_id, "status": "progress"})
            logger.debug("zmq_publishing_progress", container_id=container_id)
            await self.pub_socket.send(msg.encode())
        except Exception:
            logger.warning(
                "zmq_publish_progress_failed",
                container_id=container_id,
                exc_info=True,
            )

    async def publish_build_complete(self, container_id: str) -> None:
        """Publish build completion message on PUB socket."""
        if self.pub_socket is None:
            logger.warning("zmq_pub_not_initialized")
            return
        try:
            msg = json.dumps({"container_id": container_id, "status": "done"})
            logger.debug("zmq_publishing_done", container_id=container_id)
            await self.pub_socket.send(msg.encode())
            logger.info("zmq_published_done", container_id=container_id)
        except Exception:
            logger.error(
                "zmq_publish_done_failed",
                container_id=container_id,
                exc_info=True,
            )

    async def start_request_handler(self) -> None:
        """Handle build status queries on REP socket (blocks until cancelled)."""
        if self.rep_socket is None:
            logger.warning("zmq_rep_not_initialized")
            return

        logger.info("zmq_rep_handler_starting")
        try:
            while True:
                logger.debug("zmq_rep_waiting")
                query_bytes = await self.rep_socket.recv()
                logger.debug("zmq_rep_received", bytes=len(query_bytes))
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
                            "zmq_rep_stored_pid",
                            container_id=container_id,
                            pid=pid,
                            bundle=bundle,
                        )
                    logger.info(
                        "zmq_rep_query",
                        container_id=container_id,
                        pid=pid,
                        bundle=bundle,
                    )

                    status = await self.query_build_status(container_id)
                    logger.debug(
                        "zmq_rep_responding",
                        container_id=container_id,
                        status=status,
                    )
                    response = json.dumps(status)
                    await self.rep_socket.send(response.encode())
                    logger.debug("zmq_rep_response_sent")
                except Exception:
                    logger.error("zmq_rep_query_error", exc_info=True)
                    await self.rep_socket.send(b'{"error":"internal error"}')
        except asyncio.CancelledError:
            logger.info("zmq_rep_handler_cancelled")
        except Exception:
            logger.error("zmq_rep_handler_error", exc_info=True)

    def shutdown(self) -> None:
        """Terminate ZeroMQ context."""
        if self.context is not None:
            self.context.term()
            logger.debug("zmq_context_terminated")
