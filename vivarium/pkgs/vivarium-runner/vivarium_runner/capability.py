"""What a guest's process offers the host beside its agent.

A backend fills in the ones it has when it launches a guest, and leaves
the rest `None`: UML's management console is a balloon, QEMU's monitor is
a balloon, the power switch and, with `vivarium.display`, a screen. krun
and a container offer none. `Machine` asks for a capability by name and
says which guest lacks it, so it never needs to know the backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from .display import InputEvent


class ControlError(RuntimeError):
    """A guest's control channel refused a request, or did not answer one."""


class Channel(Protocol):
    """One connection to a guest's process, which the capabilities share."""

    async def close(self) -> None: ...


class Balloon(Protocol):
    async def balloon(self, delta: int) -> None:
        """Move the guest's memory by *delta* bytes, never above what it booted with."""
        ...


class Power(Protocol):
    """The power switch of a guest that starts with its CPUs stopped."""

    async def run(self) -> None:
        """Start the CPUs, and from then on record why the guest stops."""
        ...

    async def shutdown_cause(self) -> str | None:
        """Why the guest's process ended, such as `guest-reset`, when it said so."""
        ...


class Framebuffer(Protocol):
    async def screendump(self, path: Path, png: bool = True) -> None: ...

    async def sendkey(self, keys: str) -> None: ...

    async def input(self, events: list[InputEvent]) -> None: ...


@dataclass(frozen=True)
class Screen:
    device: Framebuffer
    width: int
    height: int
    ocr: tuple[Path, Path] | None
    """tesseract and magick, when the guest's `vivarium.display` names them."""


@dataclass(frozen=True)
class Controls:
    """One boot's capabilities. `channel` is the connection they share, closed once."""

    channel: Channel | None = None
    balloon: Balloon | None = None
    power: Power | None = None
    screen: Screen | None = None
