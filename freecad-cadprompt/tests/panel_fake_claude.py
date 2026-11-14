"""A pretend `claude` program for the panel tests: the real one is never started.

It writes what it was started with (arguments, the ANTHROPIC_* and bridge
variables of its environment, the prompt from stdin) as one JSON line to the
file named in PANEL_FAKE_CLAUDE_LOG, then answers like Claude Code would:
`auth status` with a small JSON, everything else with a stream-json result.
"""

import json
import os
import sys

args = sys.argv[1:]
auth = args[:2] == ["auth", "status"]
keep = ("ANTHROPIC_", "FREECAD_MCP_", "CADPROMPT_")
record = {"argv": args, "cwd": os.getcwd(), "stdin": "" if auth else sys.stdin.read(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(keep)},
          "path0": os.environ.get("PATH", "").split(os.pathsep)[0]}
with open(os.environ["PANEL_FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")

if auth:
    print(json.dumps({"loggedIn": os.environ.get("PANEL_FAKE_LOGGED_IN", "1") == "1",
                      "projectsDirectory": os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "projects")}))
else:
    for event in (
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fake answer to: " + record["stdin"]},
            {"type": "tool_use", "id": "t1", "name": "mcp__freecad__execute_code",
             "input": {"code": "doc.recompute()"}}]}},
        {"type": "result", "is_error": False, "result": "done", "session_id": "fake-session-1"},
    ):
        print(json.dumps(event), flush=True)
