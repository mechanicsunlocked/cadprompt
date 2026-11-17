# RUN: python
"""Tests of cadprompt/mcp_server.py: the MCP server that Claude Code talks to. Plain python3:

    python3 tests/test_mcp_server.py

The tools are made up here; FreeCAD is not needed. A raw HTTP client plays
Claude Code, a web page, and a program that guesses.
"""

import base64
import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

from cadprompt import local_prompt, mcp_server       # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"made up, not a real picture"
TOOLS = [
    {"name": "run_python", "description": "run code",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "get_view", "description": "a picture", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "takes a while", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "broken", "description": "raises", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "plain", "description": "gives a text", "inputSchema": {"type": "object", "properties": {}}},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.release = threading.Event()
        self.srv = mcp_server.Server(TOOLS, self.call_tool, name="test-server", version="7",
                                     instructions="Use the tools.")
        self.srv.start()

    def tearDown(self):
        self.release.set()
        self.srv.stop()

    def call_tool(self, name, args):
        self.calls.append((name, args, threading.current_thread().name))
        if name == "run_python":
            failed = "boom" in args.get("code", "")
            return [mcp_server.text_item("ERROR: boom" if failed else "CHANGES:\n+ new Box")], failed
        if name == "get_view":
            return [mcp_server.image_item(PNG)], False
        if name == "slow":
            self.release.wait(10)
            return [mcp_server.text_item("late")], False
        if name == "broken":
            raise RuntimeError("the tool fell over")
        return "just a text"

    def send(self, body, method="POST", path="/mcp", token=True, headers=None, host=None, raw=False):
        """(status, headers, parsed JSON or the text) of one request."""
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=15)
        head = {"Accept": "application/json, text/event-stream"}
        if body is not None:
            head["Content-Type"] = "application/json"
        if token is True:
            head["Authorization"] = "Bearer " + self.srv.token
        elif token:
            head["Authorization"] = token
        if host is not None:
            head["Host"] = host
        head.update(headers or {})
        data = body if raw or body is None else json.dumps(body).encode("utf-8")
        try:
            conn.request(method, path, data, head)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
            got = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()
        try:
            return resp.status, got, json.loads(text) if text else None
        except ValueError:
            return resp.status, got, text

    def rpc(self, method, params=None, ident=1, **kw):
        body = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            body["params"] = params
        return self.send(body, **kw)


class Protocol(Base):
    def test_initialize_answers_with_the_clients_version(self):
        status, head, body = self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {"roots": {}},
            "clientInfo": {"name": "claude-code", "version": "2"}})
        self.assertEqual(status, 200)
        self.assertEqual(head["content-type"], "application/json")
        self.assertEqual(body, {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-server", "version": "7"}, "instructions": "Use the tools."}})
        self.assertNotIn("mcp-session-id", head)             # no session: nothing to keep or to end

    def test_initialize_with_an_older_version_keeps_it(self):
        for version in ("2025-03-26", "2024-11-05"):
            _, _, body = self.rpc("initialize", {"protocolVersion": version})
            self.assertEqual(body["result"]["protocolVersion"], version)

    def test_initialize_with_an_unknown_version_answers_with_the_newest(self):
        for version in ("2099-01-01", "1.0", None, 5):
            _, _, body = self.rpc("initialize", {"protocolVersion": version})
            self.assertEqual(body["result"]["protocolVersion"], mcp_server.VERSIONS[0])
        self.assertEqual(mcp_server.VERSIONS[0], max(mcp_server.VERSIONS))

    def test_a_notification_is_202_without_a_body(self):
        for method in ("notifications/initialized", "notifications/cancelled", "notifications/anything"):
            status, head, body = self.send({"jsonrpc": "2.0", "method": method})
            self.assertEqual((status, body, head["content-length"]), (202, None, "0"), method)

    def test_an_answer_of_the_client_is_202_too(self):
        status, _, body = self.send({"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertEqual((status, body), (202, None))

    def test_ping(self):
        self.assertEqual(self.rpc("ping", ident="p-1")[2], {"jsonrpc": "2.0", "id": "p-1", "result": {}})

    def test_tools_list(self):
        status, _, body = self.rpc("tools/list", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], {"tools": TOOLS})
        for tool in body["result"]["tools"]:
            self.assertEqual(sorted(tool), ["description", "inputSchema", "name"])
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_call_with_a_text(self):
        status, _, body = self.rpc("tools/call", {"name": "run_python", "arguments": {"code": "box()"}}, ident=7)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"jsonrpc": "2.0", "id": 7, "result": {
            "content": [{"type": "text", "text": "CHANGES:\n+ new Box"}], "isError": False}})
        self.assertEqual(self.calls[0][:2], ("run_python", {"code": "box()"}))

    def test_a_tool_that_failed_is_a_result_with_isError(self):
        _, _, body = self.rpc("tools/call", {"name": "run_python", "arguments": {"code": "boom"}})
        self.assertEqual(body["result"], {"content": [{"type": "text", "text": "ERROR: boom"}], "isError": True})

    def test_tools_call_with_a_picture(self):
        _, _, body = self.rpc("tools/call", {"name": "get_view"})          # no arguments at all
        item = body["result"]["content"][0]
        self.assertEqual(sorted(item), ["data", "mimeType", "type"])
        self.assertEqual((item["type"], item["mimeType"]), ("image", "image/png"))
        self.assertEqual(base64.b64decode(item["data"]), PNG)
        self.assertIs(body["result"]["isError"], False)
        self.assertEqual(self.calls[0][:2], ("get_view", {}))

    def test_a_tool_that_gives_a_plain_text(self):
        _, _, body = self.rpc("tools/call", {"name": "plain", "arguments": {}})
        self.assertEqual(body["result"], {"content": [{"type": "text", "text": "just a text"}], "isError": False})

    def test_a_tool_that_raises_gives_a_text_the_model_can_read(self):
        status, _, body = self.rpc("tools/call", {"name": "broken", "arguments": {}})
        self.assertEqual(status, 200)
        self.assertIs(body["result"]["isError"], True)
        self.assertIn("the tool fell over", body["result"]["content"][0]["text"])
        self.assertIn("RuntimeError", body["result"]["content"][0]["text"])

    def test_an_unknown_tool_is_an_error_and_nothing_runs(self):
        for name in ("read_project_file", "execute_code", "", None, 5):
            status, _, body = self.rpc("tools/call", {"name": name, "arguments": {}})
            self.assertEqual(status, 200)
            self.assertEqual(body["error"]["code"], -32602, name)
            self.assertIn("Unknown tool", body["error"]["message"])
        self.assertEqual(self.calls, [])

    def test_arguments_that_are_no_object_are_an_error(self):
        _, _, body = self.rpc("tools/call", {"name": "run_python", "arguments": "box()"})
        self.assertEqual(body["error"]["code"], -32602)
        self.assertEqual(self.calls, [])

    def test_an_unknown_method_is_error_32601(self):
        for method in ("resources/list", "prompts/list", "tools/delete", "shutdown"):
            status, _, body = self.rpc(method, ident=9)
            self.assertEqual(status, 200)
            self.assertEqual(body, {"jsonrpc": "2.0", "id": 9, "error": {
                "code": -32601, "message": "Method not found: %s" % method}})

    def test_malformed_json(self):
        for raw in (b"{not json", b"", b"\xff\xfe", b"[1, 2"):
            status, head, body = self.send(raw, raw=True)
            self.assertEqual(status, 400, raw)
            self.assertEqual(body["error"]["code"], -32700)
            self.assertIsNone(body["id"])
            self.assertEqual(head["content-type"], "application/json")

    def test_json_that_is_no_request(self):
        for message in (5, "text", None, {}, {"jsonrpc": "1.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1},
                        {"jsonrpc": "2.0", "id": 1, "method": 7}, {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
                        {"jsonrpc": "2.0", "id": True, "method": "ping"}, []):
            status, _, body = self.send(json.dumps(message).encode("ascii"), raw=True)
            self.assertEqual((status, body["error"]["code"]), (400, -32600), message)
        self.assertEqual(self.calls, [])

    def test_a_batch_of_an_older_client(self):
        status, _, body = self.send([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                     {"jsonrpc": "2.0", "id": 2, "method": "nope"}])
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body], [1, 2])
        self.assertEqual(body[1]["error"]["code"], -32601)
        status, _, body = self.send([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual((status, body), (202, None))

    def test_the_protocol_version_header(self):
        for version in mcp_server.VERSIONS:
            self.assertEqual(self.rpc("ping", headers={"MCP-Protocol-Version": version})[0], 200)
        status, _, _ = self.rpc("ping", headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)

    def test_claude_code_as_it_really_connects(self):
        # What Claude Code 2.1.326 sent, in this order (written down from a real run, 2026-11-17).
        # 1. A question in a newer form of the protocol. This server does not speak it and must say
        #    400, so that Claude Code goes on with the form that every version knows.
        probe = {"jsonrpc": "2.0", "id": "server-discover-probe-1", "method": "server/discover", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "2.1.326"},
            "io.modelcontextprotocol/clientCapabilities": {"roots": {"listChanged": True}}}}}
        agent = {"User-Agent": "claude-code/2.1.326 (sdk-cli)", "Connection": "keep-alive"}
        status, _, _ = self.send(probe, headers=dict(agent, **{"MCP-Protocol-Version": "2026-07-28"}))
        self.assertEqual(status, 400)
        # 2. initialize without that header, with the newest version Claude Code knows
        status, head, body = self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {"roots": {"listChanged": True}, "elicitation": {}},
            "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.326"}}}, headers=agent)
        self.assertEqual((status, body["result"]["protocolVersion"]), (200, "2025-06-18"))
        self.assertEqual(head["content-type"], "application/json")
        # 3. from here on every request names the version that this server answered with
        agent["MCP-Protocol-Version"] = "2025-06-18"
        status, _, body = self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=agent)
        self.assertEqual((status, body), (202, None))
        # 4. it asks for a stream of messages from the server; there is none
        status, _, _ = self.send(None, method="GET", headers=dict(agent, Accept="text/event-stream"))
        self.assertEqual(status, 405)
        status, _, body = self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=agent)
        self.assertEqual((status, len(body["result"]["tools"])), (200, len(TOOLS)))
        status, _, body = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "run_python", "arguments": {"code": "box()"}, "_meta": {"progressToken": 2}}}, headers=agent)
        self.assertEqual((status, body["result"]["isError"]), (200, False))
        self.assertEqual(self.calls[0][:2], ("run_python", {"code": "box()"}))

    def test_the_newer_question_without_its_header_is_an_unknown_method(self):
        _, _, body = self.rpc("server/discover", {"_meta": {}}, ident="probe")
        self.assertEqual(body["error"]["code"], -32601)

    def test_brackets_nested_very_deep_are_malformed_json(self):
        status, _, body = self.send(b"[" * 100000 + b"]" * 100000, raw=True)
        self.assertEqual((status, body["error"]["code"]), (400, -32700))

    def test_every_answer_closes_its_connection(self):
        for status, head, _ in (self.rpc("ping"), self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                                self.send(None, method="GET")):
            self.assertEqual(head.get("connection"), "close", status)


class Refused(Base):
    """Everything here must end before a tool runs."""

    def tearDown(self):
        self.assertEqual(self.calls, [])
        super().tearDown()

    CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_python", "arguments": {"code": "box()"}}}

    def test_get_is_405(self):
        status, head, _ = self.send(None, method="GET", headers={"Accept": "text/event-stream"})
        self.assertEqual((status, head.get("allow")), (405, "POST"))

    def test_delete_is_405(self):
        status, head, _ = self.send(None, method="DELETE")
        self.assertEqual((status, head.get("allow")), (405, "POST"))

    def test_other_methods_are_405(self):
        for method in ("PUT", "PATCH", "HEAD"):
            self.assertEqual(self.send(None, method=method)[0], 405, method)

    def test_without_a_token_is_401(self):
        status, head, body = self.send(self.CALL, token=False)
        self.assertEqual(status, 401)
        self.assertEqual(head.get("www-authenticate"), "Bearer")
        self.assertNotIn(self.srv.token, str(body))

    def test_with_a_wrong_token_is_401(self):
        token = self.srv.token
        for given in ("Bearer wrong", "Bearer " + token[:-1], "Bearer " + token + "x", "Bearer", "Bearer ",
                      "Basic " + token, token, "Bearer " + token.swapcase(), "Bearer none", "Bearer é"):
            status, _, _ = self.send(self.CALL, token=given.encode("utf-8").decode("latin-1"))
            self.assertEqual(status, 401, given)

    def test_the_token_is_needed_for_every_method_and_path(self):
        self.assertEqual(self.send(None, method="GET", token=False)[0], 401)
        self.assertEqual(self.send(None, method="DELETE", token=False)[0], 401)
        self.assertEqual(self.send(self.CALL, path="/other", token=False)[0], 401)
        self.assertEqual(self.rpc("initialize", {}, token=False)[0], 401)
        self.assertEqual(self.rpc("tools/list", token=False)[0], 401)

    def test_the_token_is_not_taken_from_the_address(self):
        status, _, _ = self.send(self.CALL, path="/mcp?token=" + self.srv.token, token=False)
        self.assertEqual(status, 401)

    def test_an_origin_header_is_403_also_with_the_right_token(self):
        for origin in ("https://evil.example", "http://127.0.0.1:%d" % self.srv.port, "http://localhost", "null", ""):
            status, _, _ = self.send(self.CALL, headers={"Origin": origin})
            self.assertEqual(status, 403, origin)

    def test_a_browser_asking_first_is_403(self):
        status, head, _ = self.send(None, method="OPTIONS", token=False, headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual(status, 403)
        self.assertFalse([k for k in head if k.startswith("access-control")])

    def test_a_wrong_host_is_403_also_with_the_right_token(self):
        port = self.srv.port
        for host in ("evil.example", "evil.example:%d" % port, "127.0.0.1", "127.0.0.1:%d" % (port + 1), "localhost",
                     "127.0.0.1.evil.example:%d" % port, "[::1]:%d" % port, "0.0.0.0:%d" % port, ""):
            status, _, _ = self.send(self.CALL, host=host)
            self.assertEqual(status, 403, host)

    def test_the_two_names_of_this_computer_are_allowed(self):
        for host in ("127.0.0.1:%d" % self.srv.port, "localhost:%d" % self.srv.port, "LOCALHOST:%d" % self.srv.port):
            self.assertEqual(self.rpc("ping", host=host)[0], 200, host)

    def test_another_path_is_404(self):
        for path in ("/", "/mcp/", "/mcp2", "/sse", "/MCP"):
            self.assertEqual(self.send(self.CALL, path=path)[0], 404, path)
        self.assertEqual(self.rpc("ping", path="/mcp?x=1")[0], 200)

    def test_a_body_that_is_too_large_is_413_and_is_not_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=15)
        try:
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", "Bearer " + self.srv.token)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(mcp_server.MAX_BODY + 1))
            conn.endheaders()
            t0 = time.time()
            resp = conn.getresponse()            # the answer comes though no byte of the body was sent
            self.assertEqual(resp.status, 413)
            self.assertLess(time.time() - t0, 5)
        finally:
            conn.close()

    def test_a_body_of_the_largest_size_is_taken(self):
        pad = " " * (mcp_server.MAX_BODY - len(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})))
        raw = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + pad).encode("ascii")
        self.assertEqual(len(raw), mcp_server.MAX_BODY)
        self.assertEqual(self.send(raw, raw=True)[0], 200)

    def test_no_length_or_a_wrong_one(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=15)
        try:
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", "Bearer " + self.srv.token)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 411)
        finally:
            conn.close()
        self.assertEqual(self.send(self.CALL, headers={"Content-Length": "-5"})[0], 400)

    def test_a_form_of_a_web_page_is_415(self):
        raw = json.dumps(self.CALL).encode("utf-8")
        for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            self.assertEqual(self.send(raw, raw=True, headers={"Content-Type": kind})[0], 415, kind)
        self.assertEqual(self.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(), raw=True,
                                   headers={"Content-Type": "application/json; charset=utf-8"})[0], 200)


class LifeCycle(Base):
    def test_it_listens_on_this_computer_only(self):
        self.assertEqual(self.srv._httpd.server_address[0], "127.0.0.1")
        self.assertEqual(self.srv.url, "http://127.0.0.1:%d/mcp" % self.srv.port)
        self.assertGreater(self.srv.port, 1023)

    def test_every_start_has_a_new_token_and_the_old_one_is_dead(self):
        old = self.srv.token
        self.assertGreaterEqual(len(old), 43)                # 32 random bytes
        self.srv.stop()
        self.assertEqual((self.srv.token, self.srv.url), ("", ""))
        self.assertFalse(self.srv.token_fits(""))            # a stopped server takes no token at all
        self.srv.start()
        self.assertNotEqual(self.srv.token, old)
        status, _, _ = self.rpc("ping", token="Bearer " + old)
        self.assertEqual(status, 401)
        self.assertEqual(self.rpc("ping")[0], 200)

    def test_the_port_is_closed_after_stop(self):
        port = self.srv.port
        self.assertTrue(self.srv.running())
        self.srv.stop()
        self.assertFalse(self.srv.running())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        self.srv.stop()                                      # twice is harmless

    def test_two_clients_at_once(self):
        got = {}

        def client(key, name):
            got[key] = self.rpc("tools/call", {"name": name, "arguments": {}}, ident=key)[2]

        slow = threading.Thread(target=client, args=("a", "slow"))
        slow.start()
        for _ in range(100):                                 # until the slow call is in its tool
            if self.calls:
                break
            time.sleep(0.02)
        t0 = time.time()
        client("b", "plain")                                 # answered while the other one still waits
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(got["b"]["result"]["content"][0]["text"], "just a text")
        self.assertNotIn("a", got)
        self.release.set()
        slow.join(5)
        self.assertEqual(got["a"]["result"]["content"][0]["text"], "late")
        self.assertEqual(got["a"]["id"], "a")
        self.assertNotEqual(self.calls[0][2], self.calls[1][2])      # each request in its own thread

    def test_many_clients_at_once(self):
        got = []

        def client(n):
            got.append(self.rpc("tools/call", {"name": "run_python", "arguments": {"code": "n = %d" % n}}, ident=n)[2])

        threads = [threading.Thread(target=client, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(m["id"] for m in got), list(range(20)))
        self.assertEqual(sorted(c[1]["code"] for c in self.calls), sorted("n = %d" % n for n in range(20)))

    def test_stop_does_not_wait_for_a_call_that_is_under_way(self):
        got = {}

        def client():
            try:
                got["answer"] = self.rpc("tools/call", {"name": "slow", "arguments": {}})
            except (OSError, http.client.HTTPException) as exc:
                got["error"] = exc

        t = threading.Thread(target=client)
        t.start()
        for _ in range(100):
            if self.calls:
                break
            time.sleep(0.02)
        t0 = time.time()
        self.srv.stop()                                      # the tool is still waiting
        self.assertLess(time.time() - t0, 3)
        self.release.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(got)                                 # the client got an end, not a hang

    @unittest.skipUnless(hasattr(signal, "SIGPIPE"), "this system has no SIGPIPE")
    def test_an_answer_to_a_closed_connection_does_not_end_the_program(self):
        # FreeCAD does not ignore SIGPIPE as Python does. With the signal as FreeCAD has it, an
        # answer written to a connection that is closed would end the whole program, and this
        # test with it: a tool call of Claude Code that is still under way when the panel closes.
        before = signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        try:
            for _ in range(3):
                del self.calls[:]
                self.release.clear()
                got = []
                t = threading.Thread(target=lambda: got.append(self.try_call("slow")))
                t.start()
                for _ in range(100):
                    if self.calls:
                        break
                    time.sleep(0.02)
                self.assertTrue(self.calls)
                self.srv.stop()                          # closes the connection of the call
                self.release.set()                       # now the tool ends and its answer is written
                t.join(5)
                time.sleep(0.2)
                self.srv.start()
            self.assertEqual(self.rpc("ping")[0], 200)   # still here, still answering
        finally:
            signal.signal(signal.SIGPIPE, before)

    def try_call(self, name):
        try:
            return self.rpc("tools/call", {"name": name, "arguments": {}})
        except (OSError, http.client.HTTPException) as exc:
            return exc

    def test_a_client_that_sends_nothing_does_not_block_the_others(self):
        idle = socket.create_connection(("127.0.0.1", self.srv.port), timeout=5)
        try:
            idle.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % self.srv.port)     # and then silence
            t0 = time.time()
            self.assertEqual(self.rpc("ping")[0], 200)
            self.assertLess(time.time() - t0, 3)
        finally:
            idle.close()


class Registry(unittest.TestCase):
    """The tools that Claude Code gets are the tools of the built-in engine."""

    def test_the_bridge_offers_the_same_tools_as_the_engine(self):
        engine = {t["function"]["name"]: t["function"] for t in local_prompt.TOOLS}
        bridge = {t["name"]: t for t in local_prompt.bridge_tools()}
        self.assertEqual(list(bridge), ["run_python", "get_document_state", "make_drawing", "get_view"])
        for name in ("run_python", "get_document_state", "make_drawing"):
            self.assertEqual(bridge[name]["description"], engine[name]["description"])
            self.assertEqual(bridge[name]["inputSchema"], engine[name]["parameters"])
        self.assertEqual(sorted(bridge["run_python"]["inputSchema"]["properties"]), ["code"])
        self.assertEqual(sorted(bridge["make_drawing"]["inputSchema"]["properties"]), ["obj_name", "sheet"])

    def test_claude_code_reads_project_files_itself(self):
        self.assertNotIn("read_project_file", local_prompt.BRIDGE)
        self.assertIn("read_project_file", [t["function"]["name"] for t in local_prompt.TOOLS])

    def test_get_view_is_for_the_bridge_only(self):
        self.assertNotIn("get_view", [t["function"]["name"] for t in local_prompt.TOOLS])
        self.assertEqual(local_prompt.bridge_tools()[-1]["inputSchema"], {"type": "object", "properties": {}})

    def test_the_tools_that_need_the_users_yes(self):
        self.assertEqual(local_prompt.CHANGING, ("run_python", "make_drawing"))

    def test_tool_names_did_not_change(self):
        self.assertEqual([t["function"]["name"] for t in local_prompt.TOOLS],
                         ["run_python", "get_document_state", "read_project_file", "make_drawing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
