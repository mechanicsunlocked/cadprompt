"""A pretend `claude` program for the panel tests: the real one is never started.

It writes what it was started with (arguments, the ANTHROPIC_*, CLAUDE_CODE_* and bridge
variables of its environment, the prompt from stdin) as one JSON line to the
file named in PANEL_FAKE_CLAUDE_LOG, then answers like Claude Code would:
`auth status` with a small JSON, everything else with a stream-json result.
The panel puts the state of the project under the user's words
(runner.with_state): the log has the words as "stdin" and that block as "state".
A session id that starts with "gone" is one it does not have any more.
A prompt that starts with "slow" is one it works on for a minute: it writes
its process id to the log first, so the test can see that Stop ends it.
A prompt that starts with "mcp:" makes it do what Claude Code does with its
MCP servers: it takes them from the .mcp.json of the folder it runs in, and
from the file named after --mcp-config; with --strict-mcp-config only from
that file. It fills in the variables and calls the FreeCAD tools of the
server `freecad`; the rest of the prompt is the code for run_python. What the
server answered goes into the log as "mcp". A server that is a program
("command") is not started: what Claude Code would start, with which
arguments and environment, goes into the log as "started".
"""

import http.client
import json
import os
import re
import sys
import time

args = sys.argv[1:]
auth = args[:2] == ["auth", "status"]
keep = ("ANTHROPIC_", "CLAUDE_CODE_", "FREECAD_MCP_", "CADPROMPT_", "PYTHON", "PANEL_BUNDLE_", "ENABLE_TOOL_SEARCH")


def expand(text):
    """${NAME} and ${NAME:-default} from the environment, as Claude Code fills them in."""
    def value(m):
        name, _, default = m.group(1).partition(":-")
        return os.environ.get(name, default)
    return re.sub(r"\$\{([^}]+)\}", value, text)


def post(url, headers, message):
    """(HTTP status, parsed answer) of one JSON-RPC message sent to the MCP server."""
    host, path = re.fullmatch(r"http://([^/]+)(/.*)", url).groups()
    conn = http.client.HTTPConnection(host, timeout=30)
    try:
        conn.request("POST", path, json.dumps(message).encode("utf-8"),
                     dict(headers, **{"Content-Type": "application/json",
                                      "Accept": "application/json, text/event-stream"}))
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(text) if text else None
        except ValueError:
            return resp.status, text
    finally:
        conn.close()


def servers():
    """The MCP servers as Claude Code started with these arguments takes them: {name: (where from, config)}."""
    found = {}
    if "--strict-mcp-config" not in args:
        try:                         # with -p, the file of the folder is used without a question
            with open(".mcp.json", encoding="utf-8") as f:
                found.update({name: ("project", one) for name, one in json.load(f)["mcpServers"].items()})
        except (OSError, ValueError, KeyError):
            pass
    if "--mcp-config" in args[:-1]:
        with open(args[args.index("--mcp-config") + 1], encoding="utf-8") as f:
            found.update({name: ("argument", one) for name, one in json.load(f)["mcpServers"].items()})
    return found


def use_tools(code):
    """What Claude Code does at its start and for one tool call, as a dict for the log."""
    out = {}
    try:
        found = servers()
        out["servers"] = {name: origin for name, (origin, _) in found.items()}
        out["started"] = [[expand(one["command"])] + [expand(a) for a in one.get("args", [])]
                          + sorted(expand(v) for v in one.get("env", {}).values())
                          for _origin, one in found.values() if "command" in one]
        out["from"], server = found["freecad"]
        url = expand(server["url"])
        headers = {k: expand(v) for k, v in server.get("headers", {}).items()}
        out["type"], out["url"] = server.get("type"), url
        out["initialize"] = post(url, headers, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pretend-claude", "version": "0"}}})
        out["initialized"] = post(url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        status, body = post(url, headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        out["tools"] = [t["name"] for t in body["result"]["tools"]] if status == 200 else [status, body]
        out["call"] = post(url, headers, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                          "params": {"name": "run_python", "arguments": {"code": code}}})
        out["no_token"] = post(url, {}, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})[0]
    except Exception as exc:
        out["error"] = repr(exc)
    return out


if not auth:                     # the prompt is UTF-8, whatever the encoding of this system is
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
words, state = ("" if auth else prompt), ""
at = words.rfind("\n\n<cadprompt-state>\n")
if at >= 0 and words.rstrip().endswith("</cadprompt-state>"):
    words, state = words[:at], words[at + 2:]
record = {"argv": args, "cwd": os.getcwd(), "stdin": words, "state": state, "pid": os.getpid(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(keep)},
          "path": os.environ.get("PATH", "").split(os.pathsep),
          "path0": os.environ.get("PATH", "").split(os.pathsep)[0]}
if record["stdin"].startswith("mcp:"):
    record["mcp"] = use_tools(record["stdin"][4:].strip())
with open(os.environ["PANEL_FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")
if record["stdin"].startswith("slow"):
    time.sleep(60)

resume = args[args.index("--resume") + 1] if "--resume" in args[:-1] else ""
if resume.startswith("gone"):
    sys.stderr.write("No conversation found with session ID: %s\n" % resume)
    sys.exit(1)

if auth:
    print(json.dumps({"loggedIn": os.environ.get("PANEL_FAKE_LOGGED_IN", "1") == "1",
                      "projectsDirectory": os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "projects")}))
else:
    out = sys.stdout.buffer      # Claude Code writes UTF-8 on every system
    for event in (
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fake answer to: " + record["stdin"]},
            {"type": "tool_use", "id": "t1", "name": "mcp__freecad__run_python",
             "input": {"code": "doc.recompute()"}}]}},
        {"type": "result", "is_error": False, "result": "done", "session_id": "fake-session-1"},
    ):
        out.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()
