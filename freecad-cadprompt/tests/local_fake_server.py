"""A fake OpenAI-compatible server for the local-engine tests: scripted replies, no model.

    srv = FakeServer([call("run_python", {"code": "..."}), answer("Done.")])
    ... point the engine at srv.url ...
    srv.requests[0]["body"]["messages"]     what the engine sent
    srv.close()

A reply is: a message dict (see answer() and call()), error(status, text),
slow(seconds, reply), or a function body -> reply. When the script is used up
the server answers "(script ended)", so a test never hangs.
"""

import http.server
import json
import select
import socket
import threading
import time


def answer(text, reasoning=None):
    msg = {"role": "assistant", "content": text}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return msg


def call(name, args, call_id=None, text="", reasoning=None):
    msg = answer(text, reasoning)
    msg["tool_calls"] = [{"id": call_id or "call_%s" % name, "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}}]
    return msg


def error(status, text, code=None):
    return ("error", status, {"error": {"message": text, "type": "invalid_request_error", "code": code}})


def slow(seconds, reply):
    return ("slow", seconds, reply)


class FakeServer:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []       # {"path", "headers", "body"} of every POST
        self.dropped = 0         # slow replies the client did not wait for
        self.overrun = 0         # requests after the end of the script
        self._lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                with owner._lock:
                    owner.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                           "body": body})
                    if owner.script:
                        reply = owner.script.pop(0)
                    else:
                        owner.overrun += 1
                        reply = answer("(script ended)")
                if callable(reply):
                    reply = reply(body)
                if isinstance(reply, tuple) and reply[0] == "slow":
                    if not self._wait(reply[1]):
                        with owner._lock:
                            owner.dropped += 1
                        return
                    reply = reply[2]
                if isinstance(reply, tuple) and reply[0] == "error":
                    self._send(reply[1], reply[2])
                else:
                    self._send(200, {"id": "x", "object": "chat.completion", "model": body.get("model"),
                                     "choices": [{"index": 0, "message": reply, "finish_reason": "stop"}]})

            def _wait(self, seconds):
                """Sleep like a slow model. False when the client hung up in that time."""
                end = time.time() + seconds
                while time.time() < end:
                    ready, _, _ = select.select([self.connection], [], [], 0.02)
                    if ready:
                        try:
                            if self.connection.recv(1, socket.MSG_PEEK) == b"":
                                return False
                        except OSError:
                            return False
                return True

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True)
        self._thread.start()

    def add(self, *replies):
        with self._lock:
            self.script.extend(replies)

    def bodies(self):
        return [r["body"] for r in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
