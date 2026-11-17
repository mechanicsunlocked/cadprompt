# RUN: manual
"""One message to the REAL Claude Code through the real panel, in a FreeCAD GUI without a display.

It uses your Claude account (a short run with a small model), so run_all.py
never starts it. Switch it on yourself:

    FREECAD_USER_HOME=<fresh folder> PANEL_REAL_CLAUDE=1 QT_QPA_PLATFORM=offscreen freecad tests/claude_real_run.py

Claude Code must be installed and signed in. The test makes a project from the
template, sends "make a box" and checks three things: the tool call of Claude
Code reached the server inside this FreeCAD, the box is in the document, and
the answer names its volume. The report goes to
<FREECAD_USER_HOME>/claude_real_run.log; it also shows what Claude Code sent
to the server, which is the record of what the protocol needs.
PANEL_REAL_CLAUDE_MODEL picks the model (default: haiku).
"""

import os
import re
import sys
import threading
import time
import traceback

if not os.environ.get("FREECAD_USER_HOME"):
    os.write(1, b"Set FREECAD_USER_HOME to a fresh folder first.\n")
    os._exit(2)
if os.environ.get("PANEL_REAL_CLAUDE") != "1":
    os.write(1, b"This test uses your Claude account. Set PANEL_REAL_CLAUDE=1 to run it.\n")
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import FreeCAD as App                                   # noqa: E402
from PySide import QtCore, QtWidgets                    # noqa: E402

QtCore.QLoggingCategory.setFilterRules("default.warning=false")     # see test_local_engine.py

HOME = os.environ["FREECAD_USER_HOME"]
LOG = os.path.join(HOME, "claude_real_run.log")
open(LOG, "w", encoding="utf-8").close()
PROMPT = "Make a box 20 x 30 x 10 mm named TestBox, then tell me its volume."
passed, failed = [], []


def say(line):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        os.write(1, (line + "\n").encode("utf-8"))
    except OSError:
        pass


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    say(("ok    " if ok else "FAIL  ") + name + ("" if ok else "   <- %s" % (detail,)))


def pump(cond=lambda: False, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def main():
    from cadprompt import bridge, local_tools, mcp_server, panel, paths, projects

    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(HOME, "projects"))
    p.SetString("Backend", "account")
    p.SetString("ModelOverride", os.environ.get("PANEL_REAL_CLAUDE_MODEL") or "haiku")
    project = projects.create("Real run")

    # what reaches the server, and where each tool runs
    seen, ran = [], []
    real_handle, real_request, real_run_tool = mcp_server.Server.handle, mcp_server._Handler._request, local_tools.run_tool

    def handle(self, message):
        status, answer = real_handle(self, message)
        if isinstance(message, dict):
            seen.append((message.get("method"), message.get("params"), status))
        return status, answer

    def request(self):
        head = {k: self.headers.get(k) for k in ("Accept", "Content-Type", "MCP-Protocol-Version", "Mcp-Session-Id",
                                                 "User-Agent", "Host", "Connection") if self.headers.get(k)}
        head["Authorization"] = "Bearer (%d characters)" % len((self.headers.get("Authorization") or "")[7:])
        say("      %s %s  %s" % (self.command, self.path, head))
        try:
            return real_request(self)
        except mcp_server._Http as exc:
            body = b""
            if exc.status == 400:                # what did Claude Code try that this server does not speak?
                try:
                    body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 600))
                except (OSError, ValueError):
                    pass
            say("        -> refused with %d (%s) %s" % (exc.status, exc.text, body.decode("utf-8", "replace")))
            raise

    def run_tool(project_, name, args):
        ran.append((name, dict(args), threading.current_thread() is threading.main_thread()))
        return real_run_tool(project_, name, args)
    mcp_server.Server.handle, mcp_server._Handler._request, local_tools.run_tool = handle, request, run_tool

    pan = panel.Panel()
    pump(lambda: pan.claude is not None, 20)
    pump(timeout=5)                              # `claude auth status` answers
    setup = [t for k, t in pan.entries if k == "setup"]
    if not pan.claude:
        check("Claude Code is installed", False, str(setup))
        return
    if any("not signed in" in t for t in setup):
        check("Claude Code is signed in", False, str(setup))
        return
    check("the panel is ready for the Claude account; nothing listens yet", not setup and not bridge.running(), str(setup))

    for d in list(App.listDocuments()):
        App.closeDocument(d)
    results = []
    pan.run.finished.connect(lambda res: results.append(dict(res)))
    t0 = time.time()
    pan.send(PROMPT)
    check("the message started Claude Code and the server", pan.send_btn.text() == "Stop" and bridge.running())
    done = pump(lambda: bool(results) and pan.send_btn.text() == "Send", 240)
    say("      the run took %.0f s" % (time.time() - t0))
    for kind, text in pan.entries:
        say("      [%s] %s" % (kind, text.replace("\n", "\n            ")))
    res = results[-1] if results else {}
    say("      result: %s" % {k: res.get(k) for k in ("is_error", "subtype", "num_turns", "total_cost_usd",
                                                      "permission_denials", "kind") if k in res})
    if not done or res.get("is_error"):
        say("      stderr of Claude Code: %s" % pan.run._err.decode("utf-8", "replace")[-2000:])
    check("the run ended without an error", done and res.get("is_error") is False, str(res)[:600])

    methods = [m for m, _, _ in seen]
    say("      requests to the server: %s" % methods)
    for method, params, status in seen:
        if method == "initialize":
            say("      initialize: %s" % params)
    check("Claude Code connected to the server in this FreeCAD: initialize, tools/list, tools/call",
          methods[:1] == ["initialize"] and "tools/list" in methods and "tools/call" in methods, str(methods))
    check("its tool call ran in FreeCAD's main thread", bool(ran) and all(r[2] for r in ran)
          and "run_python" in [r[0] for r in ran], str(ran)[:400])
    def is_box(obj):
        """A solid of 20 x 30 x 10 mm. Claude can build it as a Part box or as a body with a pad."""
        try:
            shape = obj.Shape
            size = sorted(round(v, 3) for v in (shape.BoundBox.XLength, shape.BoundBox.YLength, shape.BoundBox.ZLength))
            return bool(shape.Solids) and abs(shape.Volume - 6000) < 1e-3 and size == [10, 20, 30]
        except Exception:
            return False
    boxes = [(d.Label, o.Name, o.Label) for d in App.listDocuments().values() for o in d.Objects if is_box(o)]
    say("      objects that are the box (document, name, label): %s" % boxes)
    check("a solid of 20 x 30 x 10 mm with the volume 6000 mm3 is in FreeCAD, and TestBox is in its names",
          bool(boxes) and any("testbox" in " ".join(names).lower() for names in boxes),
          str([[o.Name for o in d.Objects] for d in App.listDocuments().values()]))
    answer = "\n".join(t for k, t in pan.entries if k == "claude")
    check("Claude's answer names the volume, 6000", "6000" in re.sub(r"[,.'\s  ]", "", answer), answer[-500:])
    check("no tool was blocked by the project's permission rules", not res.get("permission_denials"),
          str(res.get("permission_denials")))
    bridge.stop()


code = 1
threading.Timer(290, lambda: (say("WATCHDOG: the test hung for 290 s"), os._exit(3))).start()
try:
    main()
    code = 0 if not failed else 1
except Exception:
    say("EXCEPTION in the test itself:\n" + traceback.format_exc())
say("")
say("%d passed, %d failed%s" % (len(passed), len(failed), "" if not failed else ": " + "; ".join(failed)))
say("RESULT: " + ("OK" if code == 0 else "FAILED"))
os._exit(code)
