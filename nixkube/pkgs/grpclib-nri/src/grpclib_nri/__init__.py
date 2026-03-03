"""grpclib_nri — NRI protocol utilities built on grpclib-ttrpc.

Provides NRI-specific protocol handling like multiplexing over a single socket.
"""

from .mux import PLUGIN_SERVICE_CONN as PLUGIN_SERVICE_CONN
from .mux import RUNTIME_SERVICE_CONN as RUNTIME_SERVICE_CONN
from .mux import MuxChannelTransport as MuxChannelTransport
from .mux import NriMux as NriMux
