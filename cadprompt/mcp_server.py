"""A small MCP server inside this program, for one local client (Claude Code).

It speaks the "Streamable HTTP" transport of the Model Context Protocol in its
plainest form: the client POSTs one JSON-RPC message to /mcp and gets one JSON
object back. No event stream, no session. No Qt and no FreeCAD in here: `tools` is the list that tools/list answers, and `call_tool(name,
args)` runs one tool. It is called in the thread of the request, so the caller
(bridge.py) hands the work to FreeCAD's main thread.

Who may talk to it:
- It listens on 127.0.0.1 only, on a free port that the system picks.
- Every request needs "Authorization: Bearer <token>". The token is made new
  for each start and is kept in memory only.
- A request with an Origin header is refused. A browser sends one, Claude Code
  does not: a web page must never reach FreeCAD.
- The Host header must name this address. A web page that points its own name
  at 127.0.0.1 (DNS rebinding) sends another Host.
"""

import base64
import hmac
import http.server
import json
import re
import secrets
import socket
import socketserver
import sys
import threading

from . import main_thread

PATH = "/mcp"
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")    # protocol versions, the newest first
MAX_BODY = 4 * 1024 * 1024       # bytes of one request; the code of a run_python call is far smaller
MAX_DEPTH = 64                   # brackets inside brackets in one request; a real one has fewer than ten
READ_SECONDS = 30                # a client that sends its request slower than this is dropped
DRAIN = 1024 * 1024              # bytes of a refused request that are still taken off the line

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32700, -32600, -32601, -32602


def text_item(text):
    return {"type": "text", "text": str(text)}


def image_item(data, mime="image/png"):
    """`data`: the bytes of the picture."""
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class _Http(Exception):
    """An answer that is not a JSON-RPC result: (status, text, extra headers)."""

    def __init__(self, status, text, headers=()):
        super().__init__(text)
        self.status, self.text, self.headers = status, text, tuple(headers)


class _Listener(http.server.ThreadingHTTPServer):
    daemon_threads = True            # a request that waits for FreeCAD must not keep the program alive
    block_on_close = False           # and closing never waits for such a request
    # Windows: with this on, a second program could listen on the same port and take the requests
    allow_reuse_address = False
    allow_reuse_port = False
    request_queue_size = 64          # connections that wait for their thread; the default of 5 drops the sixth
    owner = None

    def server_bind(self):
        # Not the one of HTTPServer: it asks the name service for the name of 127.0.0.1, and on
        # some computers that takes seconds. start() is called in FreeCAD's main thread.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request, client_address):
        # A client that went away before its request was read (a reset, or a broken request line
        # and then a close) is no news. The standard server prints a traceback for it, and in
        # FreeCAD that is red text in the Report view. A mistake of ours is still printed.
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


_STRINGS = re.compile(rb'"(?:\\.|[^"\\])*"', re.S)
_BRACKETS = re.compile(rb"[\[\]{}]")


def _too_deep(raw):
    """True when the brackets of this JSON text are nested deeper than MAX_DEPTH.

    Python's reader has no fixed limit: some versions give an error at a
    thousand levels, newer ones read 100,000 levels into a list that later
    code cannot walk. The limit is ours, so each version answers the same.
    Brackets inside a text (the code of run_python) do not count.
    """
    depth = 0
    for m in _BRACKETS.finditer(_STRINGS.sub(b"", raw)):
        if m.group() in b"[{":
            depth += 1
            if depth > MAX_DEPTH:
                return True
        else:
            depth -= 1
    return False


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CADprompt"
    sys_version = ""
    timeout = READ_SECONDS

    def log_message(self, *args):    # nothing on FreeCAD's console for each request
        pass

    def setup(self):
        main_thread.quiet_pipe()     # a client that went away must not end FreeCAD
        main_thread.quiet_socket(self.request)
        super().setup()
        self.server.owner._opened(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.owner._closed(self.connection)

    def _answer(self, status, body=b"", kind="application/json", headers=()):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")      # one request for each connection: nothing stays open
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        self.close_connection = True
        try:
            try:
                message = self._request()
                status, answer = self.server.owner.handle(message)
            except _Http as exc:
                self._answer(exc.status, (exc.text + "\n").encode("utf-8"), "text/plain; charset=utf-8", exc.headers)
                self._drain()
                return
            if answer is None:
                self._answer(status)
            else:
                self._answer(status, json.dumps(answer).encode("utf-8"))
        except OSError:
            pass                     # the client went away, or the server was stopped
        except Exception as exc:     # a mistake in here must not be a silent hang for the client
            try:
                self._answer(500, ("%s\n" % exc.__class__.__name__).encode("utf-8"), "text/plain; charset=utf-8")
            except OSError:
                pass

    def _drain(self):
        """Take what a refused request still sends off the line. A connection that is
        closed with bytes not read is reset, and the client then never sees the answer."""
        if self.command not in ("POST", "PUT", "PATCH"):
            return                   # no body to wait for
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = -1
        if left < 0:
            left = DRAIN             # no length that can be believed: take what comes in a moment
        try:
            self.connection.settimeout(0.5)
            left = min(left, DRAIN)
            while left > 0:
                block = self.rfile.read1(min(left, 65536))
                if not block:
                    break
                left -= len(block)
        except (OSError, ValueError):
            pass

    do_POST = do_GET = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_HEAD = _handle

    def _request(self):
        """The JSON message of this request. Raises _Http for everything that is refused.

        The order matters: who asks is checked before anything of the request is read.
        """
        owner, headers = self.server.owner, self.headers
        hosts = headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in owner.hosts():
            raise _Http(403, "This address is not allowed.")
        if headers.get("Origin") is not None:
            raise _Http(403, "Requests from a web page are not allowed.")
        given = (headers.get("Authorization") or "").strip()
        scheme, _, token = given.partition(" ")
        if scheme.lower() != "bearer" or not owner.token_fits(token.strip()):
            raise _Http(401, "The token is missing or wrong.", [("WWW-Authenticate", "Bearer")])
        if self.path.split("?", 1)[0] != PATH:
            raise _Http(404, "The only address of this server is %s." % PATH)
        if self.command != "POST":
            # GET would open an event stream and DELETE would end a session: this server has neither
            raise _Http(405, "Only POST is allowed.", [("Allow", "POST")])
        version = headers.get("MCP-Protocol-Version")
        if version is not None and version.strip() not in VERSIONS:
            raise _Http(400, "Protocol version %s is not supported." % version.strip())
        if (headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise _Http(415, "The request must be application/json.")
        try:
            length = int(headers.get("Content-Length", ""))
        except ValueError:
            raise _Http(411, "The request needs a Content-Length.")
        if length < 0:
            raise _Http(400, "The Content-Length is not valid.")
        if length > MAX_BODY:
            raise _Http(413, "The request is larger than %d bytes." % MAX_BODY)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _Http(400, "The request ended early.")
        if _too_deep(raw):
            return _Unreadable
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):     # not JSON
            return _Unreadable


_Unreadable = object()               # a body that is not JSON: answered as a JSON-RPC error


def _hung_up(conn):
    """True when the other side closed this connection. Its one request was read, so
    nothing more comes in: the end of the stream means that the client went away."""
    try:
        before = conn.gettimeout()
        conn.settimeout(0)           # a look, never a wait
        try:
            return conn.recv(1, socket.MSG_PEEK) == b""
        finally:
            conn.settimeout(before)
    except (BlockingIOError, InterruptedError):
        return False                 # open, and nothing to read
    except OSError:
        return True                  # reset by the client, or closed by stop()


def _error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


class Server:
    """tools: [{"name", "description", "inputSchema"}, ...].
    call_tool(name, args) -> (content items, failed) or a text; see text_item and image_item.
    """

    def __init__(self, tools, call_tool, name="cadprompt", version="0", instructions=""):
        self.tools = list(tools)
        self.call_tool = call_tool
        self.info = {"name": name, "version": str(version)}
        self.instructions = instructions
        self.token = ""
        self.port = 0
        self._httpd = None
        self._thread = None
        self._open = set()               # the connections of requests that are under way
        self._lock = threading.Lock()
        self._mine = threading.local()   # of the request of this thread: conn, ident, cancelled
        self._calls = {}                 # id of a tool call that is under way -> its Event "cancelled"

    # ---- life cycle ----------------------------------------------------------
    def start(self):
        """Listen on a free port of 127.0.0.1, with a new token. Raises OSError."""
        if self._httpd is not None:
            return
        httpd = _Listener(("127.0.0.1", 0), _Handler)
        httpd.owner = self
        self.port = httpd.server_address[1]
        self.token = secrets.token_urlsafe(32)
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="cadprompt-mcp", daemon=True)
        self._thread.start()

    def running(self):
        return self._httpd is not None

    @property
    def url(self):
        return "http://127.0.0.1:%d%s" % (self.port, PATH) if self.running() else ""

    def stop(self):
        """Close the port and every open connection. Does not wait for a request that
        waits for FreeCAD: stop() is called in FreeCAD's main thread."""
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        self.token = ""                  # a request that is on its way in is refused
        httpd.shutdown()                 # ends serve_forever; that thread never waits for a tool
        httpd.server_close()
        with self._lock:
            open_now = list(self._open)
        for conn in open_now:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(2)
            self._thread = None

    def _opened(self, conn):
        self._mine.conn = conn
        with self._lock:
            self._open.add(conn)

    def _closed(self, conn):
        with self._lock:
            self._open.discard(conn)

    def client_gone(self):
        """True when nobody waits for the answer of this thread's tool call any more: the
        client closed the connection, or it sent notifications/cancelled for the request.
        For call_tool, while the tool waits (bridge.py). False in every other thread."""
        mine = self._mine
        cancelled, conn = getattr(mine, "cancelled", None), getattr(mine, "conn", None)
        return (cancelled is not None and cancelled.is_set()) or (conn is not None and _hung_up(conn))

    def _cancel(self, params):
        """notifications/cancelled: the client gave a request up. Claude Code can keep the
        connection of that request open, so this is the only word of it."""
        ident = params.get("requestId") if isinstance(params, dict) else None
        if not isinstance(ident, (str, int, float)) or isinstance(ident, bool):
            return
        with self._lock:
            cancelled = self._calls.get(ident)
        if cancelled is not None:        # only a call that is under way: the next client counts from 1 again
            cancelled.set()

    # ---- who asks ------------------------------------------------------------
    def hosts(self):
        return ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port)

    def token_fits(self, given):
        token = self.token
        return bool(token) and hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))

    # ---- JSON-RPC -------------------------------------------------------------
    def handle(self, message):
        """(HTTP status, answer or None) for one posted message."""
        if message is _Unreadable:
            return 400, _error(None, PARSE_ERROR, "Parse error: the request is not JSON")
        if isinstance(message, list):        # a batch, from a client of protocol 2025-03-26
            if not message:
                return 400, _error(None, INVALID_REQUEST, "Invalid request: an empty batch")
            answers = [a for a in map(self._one, message) if a is not None]
            return (200, answers) if answers else (202, None)
        answer = self._one(message)
        if answer is None:
            return 202, None                 # a notification or a response: taken, nothing to say
        bad = "error" in answer and answer["error"]["code"] == INVALID_REQUEST
        return (400 if bad else 200), answer

    def _one(self, message):
        """The answer to one message, or None when it wants none."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Invalid request: not a JSON-RPC 2.0 message")
        method, ident = message.get("method"), message.get("id")
        if method is None:
            if "result" in message or "error" in message:
                return None                  # the client's answer to a request: this server sends none
            return _error(ident, INVALID_REQUEST, "Invalid request: no method")
        if not isinstance(method, str):
            return _error(ident, INVALID_REQUEST, "Invalid request: the method is not a text")
        if "id" not in message:
            if method == "notifications/cancelled":
                self._cancel(message.get("params"))
            return None                      # notifications/initialized, ...
        if not isinstance(ident, (str, int, float)) or isinstance(ident, bool):
            return _error(None, INVALID_REQUEST, "Invalid request: the id is not a text or a number")
        params = message.get("params")
        self._mine.ident = ident
        try:
            result = self._result(method, params if isinstance(params, dict) else {})
        except _RpcError as exc:
            return _error(ident, exc.code, exc.message)
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def _result(self, method, params):
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                      "capabilities": {"tools": {}}, "serverInfo": dict(self.info)}
            if self.instructions:
                result["instructions"] = self.instructions
            return result
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return self._call(params)
        raise _RpcError(NO_METHOD, "Method not found: %s" % method)

    def _call(self, params):
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in [t["name"] for t in self.tools]:
            raise _RpcError(INVALID_PARAMS, "Unknown tool: %s" % name)
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise _RpcError(INVALID_PARAMS, "The arguments of %s must be an object" % name)
        mine = self._mine
        mine.cancelled = threading.Event()
        with self._lock:
            self._calls[mine.ident] = mine.cancelled
        try:
            out = self.call_tool(name, args)
        except Exception as exc:             # the model must get a text that it can act on
            out = ([text_item("ERROR in tool %s: %s: %s" % (name, exc.__class__.__name__, exc))], True)
        finally:
            with self._lock:
                if self._calls.get(mine.ident) is mine.cancelled:
                    del self._calls[mine.ident]
        items, failed = out if isinstance(out, tuple) else ([text_item(out)], False)
        return {"content": list(items), "isError": bool(failed)}
