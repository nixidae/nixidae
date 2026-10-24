"""
What a server process sets up before it serves: its Nix settings, its
nix.conf, its overlay namespace, its sweep TTL and its callbacks.

One typed value for every way a server starts. `argv()` renders it as
the command line takes it, for a server in a process of its own or
over SSH; a `multiprocessing` child takes the value itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import namespace

#: How long a silent connection lives before the server sweeps it.
DEFAULT_TTL = 120.0


@dataclass(frozen=True, slots=True)
class ServerOptions:
    #: Process settings, set as `nix --option` does, after everything
    #: else.
    settings: Mapping[str, str] = field(default_factory=dict)
    #: Read nix.conf and NIX_CONFIG as nix does. Without it the server
    #: has Nix's built-in defaults, and `settings` says what differs.
    load_config: bool = False
    #: Serve in a user namespace over an overlay store laid out under
    #: this directory (nixidae/nixidae#1060). `overlay_namespace()`
    #: gives the store a client opens there.
    overlay: str | os.PathLike[str] | None = None
    #: The overlay's lower store.
    lower_store: str = "daemon"
    #: The directory of the lower store's paths. It must hold the
    #: server's own closure: the overlay hides the rest of /nix/store.
    lower_dir: str = namespace.STORE_DIR
    ttl: float = DEFAULT_TTL
    #: Let a client hand over objects the server calls back, such as a
    #: SourceAccessor (nixidae/nixidae#738). EXPERIMENTAL, and unsafe
    #: for a state that several clients share.
    callbacks: bool = False

    def overlay_namespace(self) -> namespace.OverlayNamespace | None:
        """The overlay the server enters, or None without one."""
        if self.overlay is None:
            return None
        return namespace.OverlayNamespace.under(
            self.overlay, lower_store=self.lower_store, lower_dir=self.lower_dir
        )

    def argv(self) -> list[str]:
        """These options as `huggorm.server` takes them, after its
        positional PATH or `--stdio`."""
        arguments = ["--ttl", repr(self.ttl)]
        if self.load_config:
            arguments.append("--load-config")
        if self.overlay is not None:
            arguments += [
                "--overlay",
                os.fspath(self.overlay),
                "--lower-store",
                self.lower_store,
                "--lower-dir",
                self.lower_dir,
            ]
        if self.callbacks:
            arguments.append("--experimental-callbacks")
        for name, value in self.settings.items():
            arguments += ["--option", name, value]
        return arguments
