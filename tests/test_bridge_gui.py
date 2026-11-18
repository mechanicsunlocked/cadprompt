# RUN: freecad-gui
"""The bridge for Claude Code inside a real FreeCAD GUI, without a display:

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/test_bridge_gui.py

Client threads play Claude Code: they talk to the MCP server of this process
over HTTP while the test pumps FreeCAD's event loop. No `claude` program and
no AI service is used. The report goes to <FREECAD_USER_HOME>/test_bridge_gui.log.
"""

import base64
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import traceback

if not os.environ.get("FREECAD_USER_HOME"):
    os.write(1, b"Set FREECAD_USER_HOME to a fresh folder first.\n")
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import FreeCAD as App                                   # noqa: E402
from PySide import QtCore, QtGui, QtWidgets             # noqa: E402

# Without a display Qt warns "This plugin does not support raise()" when FreeCAD
# shows a notification, and FreeCAD 1.1 deadlocks while it reports that warning.
QtCore.QLoggingCategory.setFilterRules("default.warning=false")

import hang_report                                      # noqa: E402

HOME = os.environ["FREECAD_USER_HOME"]
LOG = os.path.join(HOME, "test_bridge_gui.log")
open(LOG, "w", encoding="utf-8").close()
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
    """Let the GUI event loop work until cond() is true. Returns cond()."""
    end = time.time() + timeout
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class Client:
    """One request to the bridge, sent from its own thread, as Claude Code would send it."""

    def __init__(self, url, token, method, params=None, ident=1):
        self.status, self.body, self.error = None, None, None
        self.url, self.token = url, token
        self.message = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            self.message["params"] = params
        self.thread = threading.Thread(target=self._send, daemon=True)
        self.thread.start()

    def _send(self):
        port = int(re.search(r":(\d+)/", self.url).group(1))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request("POST", "/mcp", json.dumps(self.message).encode("utf-8"),
                         {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                          "Authorization": "Bearer " + self.token})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
            try:
                self.body = json.loads(text) if text else None
            except ValueError:
                self.body = text                 # a refusal is a plain text
            self.status = resp.status
        except Exception as exc:                 # the server went away in the middle
            self.error = exc
        finally:
            conn.close()

    def done(self):
        return not self.thread.is_alive()

    def text(self):
        """The text of a tools/call result, or what came in its place."""
        try:
            return self.body["result"]["content"][0]["text"]
        except (TypeError, KeyError, IndexError):
            return "status %s, body %s, error %r" % (self.status, self.body, self.error)

    def failed(self):
        try:
            return self.body["result"]["isError"]
        except (TypeError, KeyError):
            return None


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def main():
    from cadprompt import approval, bridge, consent, local_prompt, local_tools, main_thread, paths, projects
    import user_yes

    paths.params().SetString("ProjectsDir", os.path.join(HOME, "projects"))
    paths.params().SetString("BaseUrl", "http://localhost:11434")     # the endpoint of the source "custom"
    project = projects.create("Bridge test")
    gui_thread = QtWidgets.QApplication.instance().thread()

    where = {}

    def ask(method, params=None, timeout=20, **kw):
        """Send a request and pump FreeCAD until the answer is there."""
        c = Client(where["url"], where["token"], method, params, **kw)
        pump(c.done, timeout)
        return c

    def tool(name, args=None, **kw):
        return ask("tools/call", {"name": name, "arguments": args or {}}, **kw)

    # the calls that wait for the main thread right now, written down in the thread of the request
    waiting = []
    real_wait = main_thread.wait

    def recording_wait(call, gone, step=0.05):
        waiting.append(call)
        return real_wait(call, gone, step)
    main_thread.wait = recording_wait

    def sent_and_waiting(client, n, seconds=20):
        """Without one look at FreeCAD's events: wait until the call is in and waits for the main thread.
        `n` is len(waiting) from before the client was made: its thread can be faster than this one."""
        end = time.time() + seconds
        while len(waiting) == n and not client.done() and time.time() < end:
            time.sleep(0.01)
        return len(waiting) > n

    # where every tool really runs
    ran = []
    real_run_tool = local_tools.run_tool

    def recording_run_tool(project_, name, args):
        ran.append((name, threading.current_thread() is threading.main_thread(),
                    QtCore.QThread.currentThread() == gui_thread, project_))
        return real_run_tool(project_, name, args)
    local_tools.run_tool = recording_run_tool

    # ---- 1: the server starts with ensure(), not before -------------------------
    say("--- 1 life of the server")
    check("nothing listens after the addon's modules are loaded", not bridge.running())
    refused = []
    for source in ("account", "custom", "local", ""):
        try:
            bridge.ensure(project, source)
            refused.append("it started for %r" % source)
        except PermissionError:
            pass
    check("before the user's yes to Claude Code, ensure() refuses and nothing listens",
          not refused and not bridge.running() and bridge._server is None, str(refused))
    # This file tests the bridge, with no panel that could ask: the user of this scratch profile
    # said Allow to Claude Code and turned "Run the AI's code without asking" on. Part 3 tests
    # that the question is asked.
    user_yes.allow_claude_code("http://localhost:11434")
    user_yes.run_without_asking()
    check("(setup) the user's yes is in the profile", consent.allowed("account", "https://api.anthropic.com")
          and consent.allowed("custom", "http://localhost:11434") and approval.auto())
    url, token = bridge.ensure(project, "account")
    where.update(url=url, token=token)
    port = int(re.search(r":(\d+)/", url).group(1))
    check("ensure() starts the server and gives (url, token)",
          bridge.running() and re.fullmatch(r"http://127\.0\.0\.1:\d+/mcp", url) is not None and len(token) >= 43,
          f"{url} {len(token)}")
    check("ensure() again: the same server, the same token", bridge.ensure(project, "account") == (url, token))
    check("the port is open on 127.0.0.1", port_open(port))

    c = ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}})
    check("initialize: the version of the client, tools, a name, and a few words on the tools",
          c.status == 200 and c.body["result"]["protocolVersion"] == "2025-06-18"
          and c.body["result"]["capabilities"] == {"tools": {}} and c.body["result"]["serverInfo"]["name"] == "cadprompt"
          and "run_python" in c.body["result"]["instructions"], c.text())
    c = ask("tools/list", {})
    names = [t["name"] for t in c.body["result"]["tools"]] if c.status == 200 else []
    check("tools/list: run_python, get_document_state, make_drawing, get_view; no read_project_file",
          names == ["run_python", "get_document_state", "make_drawing", "get_view"], str(names))
    check("tools/list is the list of local_prompt, the same that the built-in engine uses",
          c.status == 200 and c.body["result"]["tools"] == local_prompt.bridge_tools())

    # ---- 2: a tool call from another thread runs in the main thread -------------
    say("--- 2 tools")
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    code = ("box = doc.addObject('Part::Box', 'TestBox')\n"
            "box.Length, box.Width, box.Height = 20, 30, 10\n"
            "doc.recompute()\nprint('volume', box.Shape.Volume)")
    c = tool("run_python", {"code": code})
    doc = App.ActiveDocument
    box = doc.getObject("TestBox") if doc is not None else None
    check("run_python from a client thread makes the box in FreeCAD",
          box is not None and abs(box.Shape.Volume - 6000) < 1e-6, c.text())
    check("the answer is the report of run_python: output, the new object, no error",
          c.status == 200 and c.failed() is False and "volume 6000" in c.text() and "+ new TestBox" in c.text(), c.text())
    check("the tool ran in the main thread (Python's and Qt's), with the project of ensure()",
          ran == [("run_python", True, True, project)], str(ran))
    c = tool("get_document_state")
    check("get_document_state names the box and its volume", c.failed() is False and "TestBox" in c.text()
          and "volume 6000.000" in c.text(), c.text())
    n = len(doc.Objects)
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'Lost')\nraise ValueError('made up')"})
    check("a run_python call that fails: isError, the traceback as text, the change taken back",
          c.failed() is True and "ValueError: made up" in c.text() and len(doc.Objects) == n
          and doc.getObject("Lost") is None, c.text())
    c = tool("run_python", {"code": "import subprocess"})
    check("the guard of run_python holds for Claude Code too", c.failed() is True and "not allowed" in c.text(), c.text())
    c = tool("run_python", {"code": "import cadprompt.bridge as b\nprint('T', b._server.token)\n"
                                    "raise ValueError(b._server.token)"})
    check("the token of the bridge never leaves in a tool result: not in the output, not in an error text",
          c.status == 200 and len(token) >= 43 and token not in json.dumps(c.body)
          and c.text().count("(the token of the bridge, hidden)") >= 2, c.text())
    check("... and bridge.token() is the token of the server that runs", bridge.token() == token)
    del ran[:]
    c = tool("read_project_file", {"path": "refs/INDEX.md"})
    check("read_project_file is not offered: an error, and nothing runs",
          c.status == 200 and c.body.get("error", {}).get("code") == -32602 and not ran, f"{c.body} {ran}")
    # ---- 3: the user's yes, before a tool that changes things -------------------
    say("--- 3 approval")
    asked = []
    real_request = approval.request
    answer = [False]

    def recording_request(tool_name, args, source):
        asked.append((tool_name, dict(args), source, threading.current_thread() is threading.main_thread()))
        return answer[0]
    approval.request = recording_request
    del ran[:]
    n = len(doc.Objects)
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'NotWanted')"})
    check("approval.request(tool_name, args, source) is asked in the main thread before run_python",
          asked == [("run_python", {"code": "doc.addObject('Part::Box', 'NotWanted')"}, "account", True)], str(asked))
    check("the answer False: nothing runs, and the tool says that the user did not approve",
          c.status == 200 and c.text() == "The user did not approve this step." == approval.DECLINED
          and c.failed() is False and not ran and len(doc.Objects) == n and doc.getObject("NotWanted") is None,
          f"{c.text()} {ran}")
    c = tool("make_drawing", {"sheet": "A4"})
    check("make_drawing is asked for too, and does not run on False",
          asked[-1][:3] == ("make_drawing", {"sheet": "A4"}, "account") and c.text() == approval.DECLINED and not ran
          and not [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"], f"{asked[-1]} {c.text()}")
    n_asked = len(asked)
    c = tool("get_document_state")
    check("a tool that only looks is not asked for", len(asked) == n_asked and "TestBox" in c.text())
    bridge.ensure(project, "custom")
    answer[0] = True
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'Wanted')"})
    check("the answer True: the step runs; the source is the one of ensure()",
          asked[-1][2] == "custom" and doc.getObject("Wanted") is not None and "+ new Wanted" in c.text(), c.text())
    # the real function, with no panel that could ask: only the user's switch says yes
    user_yes.run_without_asking(False)
    approval.request = real_request
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'NobodyAsked')"})
    check("the real approval.request, 'run without asking' off and no panel to ask: the step does not run",
          c.text() == approval.DECLINED and doc.getObject("NobodyAsked") is None, c.text())
    user_yes.run_without_asking()
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'SwitchOn')"})
    check("... and with the user's switch on it runs", doc.getObject("SwitchOn") is not None, c.text())
    approval.request = recording_request
    bridge.ensure(project, "account")

    # ---- 4: FreeCAD stays alive while a call waits for the user ------------------
    say("--- 4 a slow call")
    ticks = []
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: ticks.append(time.time()))
    timer.start(20)
    order, others, during = [], [], []

    def slow_request(tool_name, args, source):
        """Like a dialog that waits for the user: events go on while it is open."""
        order.append("ask " + args.get("code", "")[:1])
        if not others:                           # two more requests arrive while the first one waits
            others.append(Client(url, token, "tools/call", {"name": "run_python", "arguments": {"code": "b = 2"}},
                                 ident="b"))
            others.append(Client(url, token, "tools/list", {}, ident="c"))
        end = time.time() + 1.0
        while time.time() < end:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.005)
        if not during:
            during.append((first.done(), others[0].done(), others[1].done(), others[1].status))
        order.append("answer " + args.get("code", "")[:1])
        return True
    approval.request = slow_request
    del ran[:]
    n_ticks = len(ticks)
    first = Client(url, token, "tools/call", {"name": "run_python", "arguments": {"code": "a = 1"}}, ident="a")
    pump(lambda: first.done() and len(others) == 2 and others[0].done(), 15)
    second = others[0] if others else first
    check("while a call waits for the user, a request that needs no FreeCAD is answered, and the next call waits",
          during == [(False, False, True, 200)], str(during))
    check("FreeCAD's event loop kept running during the wait", len(ticks) - n_ticks >= 15, len(ticks) - n_ticks)
    check("two calls never run inside each other: the second one waits for the first",
          order == ["ask a", "answer a", "ask b", "answer b"] and [r[0] for r in ran] == ["run_python"] * 2
          and first.failed() is False and second.failed() is False, f"{order} {ran} {first.text()} {second.text()}")
    timer.stop()
    approval.request = recording_request

    # ---- 5: Claude Code is gone: its call that did not start is dropped ----------
    say("--- 5 a call of a run that ended")
    del ran[:]
    n = len(waiting)
    c = Client(url, token, "tools/call", {"name": "run_python", "arguments": {"code": "doc.addObject('Part::Box', 'Late')"}})
    check("(setup) the call is in and waits for the main thread", sent_and_waiting(c, n), c.text())
    bridge.idle()                                # what the panel does when Claude Code ends or is stopped
    pump(c.done, 10)
    check("idle(): a call that still waited does not run", c.done() and not ran and doc.getObject("Late") is None
          and c.failed() is True and "stopped" in c.text(), f"{c.text()} {ran}")
    check("idle() leaves the server running", bridge.running() and port_open(port))
    # Windows ends Claude Code a moment after Stop, and a program that Claude Code started can
    # live longer than it: they have the token, and the server is open while the panel is.
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'AfterStop')"})
    check("idle(): a call that comes in later does not run either", not ran and doc.getObject("AfterStop") is None
          and c.status == 200 and c.failed() is True and c.text() == bridge.GONE, f"{c.text()} {ran}")
    c = tool("get_document_state")
    check("... also not a tool that only looks", not ran and c.failed() is True and c.text() == bridge.GONE, c.text())
    c = ask("tools/list", {})
    check("... the server itself still answers", c.status == 200 and len(c.body["result"]["tools"]) == 4, c.text())

    def stopping_request(tool_name, args, source):
        """Stop, or the end of Claude Code, while the question is open. Then the user says yes."""
        bridge.idle()
        return True
    approval.request = stopping_request
    bridge.ensure(project, "account")
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'YesAfterStop')"})
    check("a yes to a question of a request that was stopped meanwhile runs nothing",
          not ran and doc.getObject("YesAfterStop") is None and c.failed() is True and c.text() == bridge.GONE,
          f"{c.text()} {ran}")
    approval.request = recording_request
    bridge.ensure(project, "account")
    c = tool("run_python", {"code": "x = 1"})
    check("the next request of the user works again", c.failed() is False and len(ran) == 1, c.text())

    # ---- 6: the backup copy before the first change of a request -----------------
    say("--- 6 backup")
    model = os.path.join(project, "model", "part.FCStd")
    doc.saveAs(model)
    notes = []
    bridge.ensure(project, "account", lambda name, detail: notes.append((name, detail)))
    tool("get_document_state")
    folder = os.path.join(project, "model", ".backup")
    check("a tool that only looks makes no backup", not os.path.isdir(folder) or not os.listdir(folder))
    tool("run_python", {"code": "doc.addObject('Part::Box', 'Step1')"})
    copies = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    check("before the first change of a request the model file is copied to model/.backup, and the panel is told",
          len(copies) == 1 and re.fullmatch(r"part-\d{8}-\d{6}\.FCStd", copies[0]) is not None
          and notes == [("backup", "model/.backup/" + copies[0])], f"{copies} {notes}")
    tool("run_python", {"code": "doc.addObject('Part::Box', 'Step2')"})
    check("one copy for each request, not for each step",
          sorted(os.listdir(folder)) == copies and len(notes) == 1, str(notes))

    # ---- 6b: the document of the request, whatever the user looks at ---------------
    say("--- 6b the document of the request")
    import FreeCADGui as Gui
    other = App.newDocument("Other")
    other.addObject("Part::Box", "Theirs")
    other.recompute()
    other_file = os.path.join(HOME, "other.FCStd")       # not in the project
    other.saveAs(other_file)
    stamp = (os.path.getsize(other_file), os.path.getmtime(other_file))
    App.setActiveDocument(doc.Name)
    bridge.ensure(project, "account")
    tool("run_python", {"code": "doc.addObject('Part::Box', 'One')"})
    App.setActiveDocument("Other")               # the user clicks the other document while Claude thinks
    c = tool("run_python", {"code": "doc.addObject('Part::Cylinder', 'Two'); doc.recompute(); doc.save(); print(doc.Name)"})
    check("the user clicked another document: the next step still goes to the document of the request",
          c.failed() is False and doc.getObject("Two") is not None and doc.Name in c.text()
          and [o.Name for o in other.Objects] == ["Theirs"], c.text())
    check("... and the other document is not saved",
          (os.path.getsize(other_file), os.path.getmtime(other_file)) == stamp)
    check("... and FreeCAD's window is in that document too (get_view, make_drawing)",
          App.ActiveDocument.Name == doc.Name and Gui.ActiveDocument.Document.Name == doc.Name)

    def clicking_request(tool_name, args, source):
        App.setActiveDocument("Other")           # the user looks at the other document while the question is open
        return True
    approval.request = clicking_request
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'Three'); print(doc.Name)"})
    check("the user clicked another document while the question was open: the same",
          c.failed() is False and doc.getObject("Three") is not None and [o.Name for o in other.Objects] == ["Theirs"],
          c.text())
    approval.request = recording_request
    c = tool("run_python", {"code": "d2 = App.newDocument('Second'); d2.addObject('Part::Box', 'B'); d2.recompute()"})
    App.setActiveDocument("Other")
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'C'); print(doc.Name)"})
    check("the code went to a document of its own: the request goes on there",
          c.failed() is False and App.getDocument("Second").getObject("C") is not None
          and [o.Name for o in other.Objects] == ["Theirs"], c.text())
    App.closeDocument("Second")                  # the user closes the document of the request
    App.setActiveDocument("Other")
    del ran[:]
    n_asked = len(asked)
    c = tool("run_python", {"code": "doc.addObject('Part::Box', 'D')"})
    check("the document of the request was closed: no tool runs in another one, the user is not asked, "
          "and the answer says why", c.failed() is True and c.text() == bridge.DOC_CLOSED % "Second" and not ran
          and len(asked) == n_asked and [o.Name for o in other.Objects] == ["Theirs"], f"{c.text()} {ran}")
    c = tool("get_document_state")
    check("... also for a tool that only looks", c.failed() is True and c.text() == bridge.DOC_CLOSED % "Second", c.text())
    App.setActiveDocument(doc.Name)
    bridge.ensure(project, "account")
    c = tool("run_python", {"code": "print(doc.Name)"})
    check("the next request of the user works in the document that is active then",
          c.failed() is False and doc.Name in c.text(), c.text())
    App.closeDocument("Other")
    App.setActiveDocument(doc.Name)

    # ---- 7: stop() -----------------------------------------------------------------
    say("--- 7 stop")

    def closing_request(tool_name, args, source):
        """The panel is closed while the question is open."""
        bridge.stop()
        return True
    approval.request = closing_request
    del ran[:]
    t0 = time.time()
    c = Client(url, token, "tools/call", {"name": "run_python", "arguments": {"code": "y = 1"}})
    pump(c.done, 15)
    took = time.time() - t0
    check("stop() in the middle of a call does not lock up: the call ends, the client gets an end",
          c.done() and took < 5, f"{took:.1f} s, {c.text()}")
    check("... and the step does not run: the client is told that nothing was done, and that is true",
          not ran and "y" not in local_tools._namespace, f"{ran} {c.text()}")
    check("after stop() nothing listens: the port is closed", not bridge.running() and not port_open(port))
    approval.request = recording_request

    url2, token2 = bridge.ensure(project, "account")
    where.update(url=url2, token=token2)
    port2 = int(re.search(r":(\d+)/", url2).group(1))
    check("ensure() after stop(): a new server with a new token", bridge.running() and token2 != token and port_open(port2),
          url2)
    old = Client(url2, token, "tools/list", {})
    pump(old.done, 10)
    check("the token of the server before does not open the new one", old.status == 401, old.text())
    del ran[:]
    n = len(waiting)
    c = Client(url2, token2, "tools/call", {"name": "run_python", "arguments": {"code": "doc.addObject('Part::Box', 'AfterStop')"}})
    check("(setup) the call is in and waits for the main thread", sent_and_waiting(c, n), c.text())
    t0 = time.time()
    bridge.stop()                                # the panel closes now
    took = time.time() - t0
    pump(c.done, 10)
    pump(timeout=0.3)                            # the call that was on its way to the main thread arrives now
    check("stop() while a call waits for the main thread: back at once, the call never runs",
          took < 3 and c.done() and not ran and doc.getObject("AfterStop") is None, f"{took:.1f} s {ran} {c.text()}")
    check("... and the port is closed", not bridge.running() and not port_open(port2))
    bridge.stop()                                # twice is harmless
    bridge.idle()
    check("stop() and idle() without a server are harmless", not bridge.running())

    # ---- 8: a picture of the 3D view ---------------------------------------------
    # Last, because it is the one tool that needs OpenGL: what is before it is reported in any case.
    say("--- 8 get_view")
    url, token = bridge.ensure(project, "account")
    where.update(url=url, token=token)
    App.setActiveDocument(doc.Name)
    c = tool("get_view")
    item = (c.body or {}).get("result", {}).get("content", [{}])[0]
    if QtGui.QGuiApplication.platformName() in ("offscreen", "minimal"):
        check("get_view without a screen: a plain text that says so, FreeCAD does not hang",
              c.failed() is True and item.get("type") == "text" and "without a screen" in item.get("text", ""), str(c.body)[:300])
    else:
        data = base64.b64decode(item.get("data", "")) if item.get("type") == "image" else b""
        img = QtGui.QImage.fromData(data, "PNG")
        check("get_view: one PNG picture of the 3D view, at most %d pixels wide and high" % local_tools.VIEW_SIDE,
              c.failed() is False and item.get("mimeType") == "image/png" and data.startswith(b"\x89PNG")
              and 16 <= max(img.width(), img.height()) <= local_tools.VIEW_SIDE,
              f"{str(c.body)[:300]} {img.width()}x{img.height()}")
        say("      get_view: %d x %d pixels, %d bytes" % (img.width(), img.height(), len(data)))
    bridge.stop()

    local_tools.run_tool = real_run_tool
    approval.request = real_request
    main_thread.wait = real_wait


code = 1
threading.Timer(270, lambda: (say("WATCHDOG: the test hung for 270 s"), os._exit(3))).start()
hang_report.watch(240, HOME, say)
try:
    main()
    code = 0 if not failed else 1
except Exception:
    say("EXCEPTION in the test itself:\n" + traceback.format_exc())
say("")
say("%d passed, %d failed%s" % (len(passed), len(failed), "" if not failed else ": " + "; ".join(failed)))
say("RESULT: " + ("OK" if code == 0 else "FAILED"))
os._exit(code)
