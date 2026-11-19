"""A pretend proxy and a pretend https service for the tests of local_agent.connect().

The proxy does what the proxy of a network does for https: it reads one
"CONNECT host:port" request, opens that connection and passes bytes. It
writes down everything that it could read. The service speaks TLS with the
certificate in tls_for_tests.pem, a certificate for these tests only: its
key is public, so it protects nothing.
"""
import http.server
import os
import select
import socket
import socketserver
import ssl
import threading

PEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tls_for_tests.pem")


def client_context():
    """Trusts the test certificate and nothing else."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(PEM)
    return ctx


class Service:
    """An https server on 127.0.0.1. `seen` has (path, headers as a dict with lower-case names)."""

    def __init__(self):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                body = b'{"data": [{"id": "model-1"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(PEM)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.url = "https://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Proxy:
    """mode "pass": a tunnel. mode "login": 407, it wants a login. mode "refuse": 403.

    `requests` has the first line and the headers of each request, `clear`
    has every byte that came from the client, as the proxy saw it.
    """

    def __init__(self, mode="pass"):
        self.mode, self.requests, self.clear = mode, [], b""
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                head = b""
                while b"\r\n\r\n" not in head:
                    part = self.request.recv(4096)
                    if not part:
                        return
                    head += part
                owner.clear += head
                lines = head.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
                headers = dict((k.strip().lower(), v.strip()) for k, v in (l.split(":", 1) for l in lines[1:]))
                owner.requests.append((lines[0], headers))
                if owner.mode != "pass" or not lines[0].startswith("CONNECT "):
                    status = "407 Proxy Authentication Required" if owner.mode == "login" else "403 Forbidden"
                    self.request.sendall(("HTTP/1.1 %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                                          % status).encode("ascii"))
                    return
                host, port = lines[0].split()[1].rsplit(":", 1)
                try:
                    far = socket.create_connection((host, int(port)), 5)
                except OSError:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                try:
                    while True:
                        ready, _, _ = select.select([self.request, far], [], [], 10)
                        if not ready:
                            return
                        for s in ready:
                            data = s.recv(65536)
                            if not data:
                                return
                            if s is self.request:
                                owner.clear += data
                                far.sendall(data)
                            else:
                                self.request.sendall(data)
                except OSError:
                    pass
                finally:
                    far.close()

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
