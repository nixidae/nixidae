"""The app: one page over HTTPS on 8443, with the certificate cert-manager put in /tls."""

from __future__ import annotations

import http.server
import ssl

BODY = b"hello from the cluster\n"


class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8443), Hello)  # noqa: S104 -- the Service reaches the pod's address
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
