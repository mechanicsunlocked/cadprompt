"""A pretend `claude` program for the panel tests: the real one is never started.

It writes what it was started with (arguments, the ANTHROPIC_* and bridge
variables of its environment, the prompt from stdin) as one JSON line to the
file named in PANEL_FAKE_CLAUDE_LOG, then answers like Claude Code would:
`auth status` with a small JSON, everything else with a stream-json result.
A session id that starts with "gone" is one it does not have any more.
A prompt that starts with "slow" is one it works on for a minute: it writes
its process id to the log first, so the test can see that Stop ends it.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
auth = args[:2] == ["auth", "status"]
keep = ("ANTHROPIC_", "FREECAD_MCP_", "CADPROMPT_", "PYTHON", "PANEL_BUNDLE_")
if not auth:                     # the prompt is UTF-8, whatever the encoding of this system is
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
record = {"argv": args, "cwd": os.getcwd(), "stdin": "" if auth else prompt, "pid": os.getpid(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(keep)},
          "path": os.environ.get("PATH", "").split(os.pathsep),
          "path0": os.environ.get("PATH", "").split(os.pathsep)[0]}
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
            {"type": "tool_use", "id": "t1", "name": "mcp__freecad__execute_code",
             "input": {"code": "doc.recompute()"}}]}},
        {"type": "result", "is_error": False, "result": "done", "session_id": "fake-session-1"},
    ):
        out.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()
