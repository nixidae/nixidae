# SPDX-License-Identifier: MIT
"""Minimal NRI plugin for testing the NRI protocol."""

import logging

from nri import nri_grpc, nri_pb2


class DummyPlugin(nri_grpc.PluginBase):
    """Minimal NRI plugin that echoes back responses without doing anything."""

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger("test.dummy_plugin")
        self.configure_called = False
        self.synchronize_called = False
        self.create_container_called = False

    async def Configure(self, stream) -> None:
        """Handle Configure request: just echo back with no event subscriptions."""
        self.logger.debug("Configure called")
        req = await stream.recv_message()
        assert req is not None
        self.logger.info(
            f"Configure: runtime={req.runtime_name!r} version={req.runtime_version!r}"
        )
        self.configure_called = True
        # Subscribe to all events using the same bit position formula as server.py:
        # bit_position = (event_value - 1), so sum for events 1..LAST
        all_events = sum(1 << (event - 1) for event in range(1, nri_pb2.Event.LAST + 1))
        await stream.send_message(nri_pb2.ConfigureResponse(events=all_events))

    async def Synchronize(self, stream) -> None:
        """Handle Synchronize request: just echo back empty response."""
        self.logger.debug("Synchronize called")
        req = await stream.recv_message()
        assert req is not None
        self.logger.info(
            f"Synchronize: pods={len(req.pods)} containers={len(req.containers)}"
        )
        self.synchronize_called = True
        await stream.send_message(nri_pb2.SynchronizeResponse())

    async def Shutdown(self, stream) -> None:
        """Handle Shutdown request."""
        self.logger.debug("Shutdown called")
        await stream.recv_message()
        await stream.send_message(nri_pb2.Empty())

    async def CreateContainer(self, stream) -> None:
        """Handle CreateContainer request: do nothing."""
        self.logger.debug("CreateContainer called")
        req = await stream.recv_message()
        assert req is not None
        self.logger.info(
            f"CreateContainer: pod={req.pod.namespace}/{req.pod.name} "  # type: ignore
            f"container={req.container.name}"  # type: ignore
        )
        self.create_container_called = True
        # Return empty adjustment (no modifications)
        adjust = nri_pb2.ContainerAdjustment()
        await stream.send_message(nri_pb2.CreateContainerResponse(adjust=adjust))

    async def UpdateContainer(self, stream) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.UpdateContainerResponse())

    async def StopContainer(self, stream) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.StopContainerResponse())

    async def UpdatePodSandbox(self, stream) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.UpdatePodSandboxResponse())

    async def StateChange(self, stream) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.Empty())

    async def ValidateContainerAdjustment(self, stream) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.ValidateContainerAdjustmentResponse())
