# SPDX-License-Identifier: MIT

import logging
from pathlib import Path

from grpclib.ttrpc import Server
from nri import api_grpc, api_pb2

from .constants import NRI_SOCKET_PATH

logger = logging.getLogger("nix-csi")


class NriPlugin(api_grpc.PluginBase):
    """Empty NRI plugin — logs every lifecycle event and passes through."""

    async def Configure(self, stream) -> None:
        req: api_pb2.ConfigureRequest = await stream.recv_message()
        logger.info(
            "NRI Configure: name=%r idx=%r",
            req.plugin_name if req else None,
            req.plugin_idx if req else None,
        )
        await stream.send_message(api_pb2.ConfigureResponse())

    async def Synchronize(self, stream) -> None:
        req: api_pb2.SynchronizeRequest = await stream.recv_message()
        logger.info(
            "NRI Synchronize: %d pods, %d containers",
            len(req.pods) if req else 0,
            len(req.containers) if req else 0,
        )
        await stream.send_message(api_pb2.SynchronizeResponse())

    async def Shutdown(self, stream) -> None:
        await stream.recv_message()
        logger.info("NRI Shutdown")
        await stream.send_message(api_pb2.Empty())

    async def CreateContainer(self, stream) -> None:
        req: api_pb2.CreateContainerRequest = await stream.recv_message()
        logger.info(
            "NRI CreateContainer: pod=%r container=%r",
            req.pod.name if req and req.pod else None,
            req.container.name if req and req.container else None,
        )
        await stream.send_message(api_pb2.CreateContainerResponse())

    async def UpdateContainer(self, stream) -> None:
        req: api_pb2.UpdateContainerRequest = await stream.recv_message()
        logger.info(
            "NRI UpdateContainer: container=%r",
            req.container.name if req and req.container else None,
        )
        await stream.send_message(api_pb2.UpdateContainerResponse())

    async def StopContainer(self, stream) -> None:
        req: api_pb2.StopContainerRequest = await stream.recv_message()
        logger.info(
            "NRI StopContainer: container=%r",
            req.container.name if req and req.container else None,
        )
        await stream.send_message(api_pb2.StopContainerResponse())

    async def UpdatePodSandbox(self, stream) -> None:
        req: api_pb2.UpdatePodSandboxRequest = await stream.recv_message()
        logger.info(
            "NRI UpdatePodSandbox: pod=%r",
            req.pod.name if req and req.pod else None,
        )
        await stream.send_message(api_pb2.UpdatePodSandboxResponse())

    async def StateChange(self, stream) -> None:
        event: api_pb2.StateChangeEvent = await stream.recv_message()
        logger.info(
            "NRI StateChange: event=%r",
            event.event if event else None,
        )
        await stream.send_message(api_pb2.Empty())

    async def ValidateContainerAdjustment(self, stream) -> None:
        req: api_pb2.ValidateContainerAdjustmentRequest = await stream.recv_message()
        logger.info(
            "NRI ValidateContainerAdjustment: container=%r",
            req.container.name if req and req.container else None,
        )
        await stream.send_message(api_pb2.ValidateContainerAdjustmentResponse())


async def nri_serve() -> None:
    sock_path = NRI_SOCKET_PATH
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    Path(sock_path).unlink(missing_ok=True)

    server = Server([NriPlugin()])

    async with server:
        await server.start(path=sock_path)
        logger.info(f"NRI plugin (ttrpc) listening on {sock_path}")
        await server.wait_closed()
