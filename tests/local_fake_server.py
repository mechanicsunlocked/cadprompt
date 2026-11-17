"""A fake OpenAI-compatible server for the local-engine tests: scripted replies, no model.

    srv = FakeServer([call("run_python", {"code": "..."}), answer("Done.")])
    ... point the engine at srv.url ...
    srv.requests[0]["body"]["messages"]     what the engine sent
    srv.close()

A reply is: a message dict (see answer() and call()), error(status, text),
slow(seconds, reply), or a function body -> reply. When the script is used up
the server answers "(script ended)", so a test never hangs.

FakeAnthropic is the same for the Anthropic Messages API (POST /v1/messages):
its replies are made with claude() and claude_error(). Its url has no /v1.
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


def error(status, text, code=None, headers=None):
    return ("error", status, {"error": {"message": text, "type": "invalid_request_error", "code": code}},
            headers or {})


def claude(text="", calls=(), thinking=False, stop_reason=None):
    """An answer of the Messages API. calls: (name, input) or (name, input, id).
    thinking: a signed thinking block comes first, as the current Claude models send it."""
    blocks = []
    if thinking:
        blocks.append({"type": "thinking", "thinking": "", "signature": "SIGNATURE-%s" % thinking})
    if text:
        blocks.append({"type": "text", "text": text})
    for c in calls:
        blocks.append({"type": "tool_use", "id": c[2] if len(c) > 2 else "toolu_%s" % c[0],
                       "name": c[0], "input": c[1]})
    return {"content": blocks, "stop_reason": stop_reason or ("tool_use" if calls else "end_turn")}


def claude_error(status, kind, text, headers=None):
    return ("error", status, {"type": "error", "error": {"type": kind, "message": text},
                              "request_id": "req_made_up"}, headers or {})


def slow(seconds, reply):
    return ("slow", seconds, reply)


class FakeServer:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []       # {"path", "headers", "body"} of every POST
        self.gets = []           # {"path", "headers"} of every GET
        self.connections = 0     # every connection that was opened, also one that sent nothing
        self.dropped = 0         # slow replies the client did not wait for
        self.overrun = 0         # requests after the end of the script
        self._lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                with owner._lock:
                    owner.connections += 1
                super().setup()

            def do_GET(self):
                with owner._lock:
                    owner.gets.append({"path": self.path, "headers": dict(self.headers.items())})
                self._send(200, owner.models())

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
                        reply = owner.ended()
                if callable(reply):
                    reply = reply(body)
                if isinstance(reply, tuple) and reply[0] == "slow":
                    if not self._wait(reply[1]):
                        with owner._lock:
                            owner.dropped += 1
                        return
                    reply = reply[2]
                if isinstance(reply, tuple) and reply[0] == "error":
                    self._send(reply[1], reply[2], reply[3] if len(reply) > 3 else {})
                else:
                    self._send(200, owner.wrap(reply, body))

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

            def _send(self, status, obj, headers=None):
                data = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    for k, v in (headers or {}).items():
                        self.send_header(k, str(v))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d%s" % (self.httpd.server_address[1], self.base)
        self._thread = threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True)
        self._thread.start()

    base = "/v1"

    def models(self):
        return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

    def ended(self):
        return answer("(script ended)")

    def wrap(self, reply, body):
        return {"id": "x", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": reply, "finish_reason": "stop"}]}

    def add(self, *replies):
        with self._lock:
            self.script.extend(replies)

    def bodies(self):
        return [r["body"] for r in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeAnthropic(FakeServer):
    """The Anthropic Messages API, scripted. Point the engine at .url (no /v1 at its end)."""

    base = ""

    def models(self):
        return {"data": [{"type": "model", "id": "claude-fake-1", "display_name": "Fake 1"},
                         {"type": "model", "id": "claude-fake-2", "display_name": "Fake 2"}],
                "has_more": False, "first_id": "claude-fake-1", "last_id": "claude-fake-2"}

    def ended(self):
        return claude("(script ended)")

    def wrap(self, reply, body):
        return dict({"id": "msg_made_up", "type": "message", "role": "assistant", "model": body.get("model"),
                     "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}, **reply)
