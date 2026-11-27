"""https://web.test/ for checks.browser: one page, with the certificate in DIR (cert.pem, key.pem)."""

from __future__ import annotations

import http.server
import ssl
import sys
from pathlib import Path

PAGE = b"<html><body>vivarium lab page</body></html>"


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


def main() -> None:
    tls = Path(sys.argv[1])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls / "cert.pem", tls / "key.pem")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 443), Page)  # noqa: S104 -- the lab reaches it on its uplink
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
