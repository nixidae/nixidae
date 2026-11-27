"""https://web.test/ for checks.browser, with the certificate in DIR (cert.pem, key.pem).

Port 443 serves one page, port 8443 another: `alt.test`'s SRV record
points at 8443, so the page says which port the proxy chose.
"""

from __future__ import annotations

import http.server
import ssl
import sys
import threading
from pathlib import Path


def page(port: int) -> type[http.server.BaseHTTPRequestHandler]:
    body = f"<html><body>vivarium lab page on {port}</body></html>".encode()

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Page


def main() -> None:
    tls = Path(sys.argv[1])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls / "cert.pem", tls / "key.pem")
    servers = []
    for port in (443, 8443):
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), page(port))  # noqa: S104 -- the lab reaches it on its uplink
        server.socket = context.wrap_socket(server.socket, server_side=True)
        servers.append(server)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
