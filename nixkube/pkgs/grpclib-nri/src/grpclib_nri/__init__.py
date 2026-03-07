"""grpclib_nri — NRI protocol utilities built on grpclib-ttrpc.

Provides NRI-specific protocol handling like multiplexing over a single socket,
a high-level NriServer class for running plugins, and an NriPlugin base class
that handles protocol boilerplate (Configure, Synchronize, Shutdown, stub handlers).
"""

from .mux import PLUGIN_SERVICE_CONN as PLUGIN_SERVICE_CONN
from .mux import RUNTIME_SERVICE_CONN as RUNTIME_SERVICE_CONN
from .mux import MuxChannelTransport as MuxChannelTransport
from .mux import NriMux as NriMux
from .plugin import NriPlugin as NriPlugin
from .plugin import make_event_bitmask as make_event_bitmask
from .server import NriServer as NriServer
