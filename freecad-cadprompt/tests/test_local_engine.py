"""End-to-end test of cadprompt/local_engine.py in a FreeCAD GUI without a display:

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/test_local_engine.py

A fake OpenAI server plays the model, so the result does not depend on any AI.
The report goes to stdout and to <FREECAD_USER_HOME>/test_local_engine.log.
FREECAD_USER_HOME keeps the test away from the user's real FreeCAD settings.
"""

import json
import os
import shutil
import sys
import threading
import time
import traceback
import types

if not os.environ.get("FREECAD_USER_HOME"):
    os.write(1, b"Set FREECAD_USER_HOME to a fresh folder first.\n")
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
ADDON = os.path.dirname(HERE)
TEMPLATE = os.path.join(os.path.dirname(ADDON), "template")
sys.path.insert(0, ADDON)
sys.path.insert(0, HERE)

import FreeCAD as App                                   # noqa: E402
from PySide import QtCore, QtWidgets                    # noqa: E402

# Without a display Qt warns "This plugin does not support raise()" when FreeCAD
# shows a notification, and FreeCAD 1.1 deadlocks while it reports that warning
# (NotificationArea locks its mutex twice). Mute the warning: a test-only problem.
QtCore.QLoggingCategory.setFilterRules("default.warning=false")

from cadprompt import local_agent, local_engine, local_tools, projects    # noqa: E402
from local_fake_server import FakeServer, answer, call, error, slow       # noqa: E402

HOME = os.environ["FREECAD_USER_HOME"]
LOG = os.path.join(HOME, "test_local_engine.log")
open(LOG, "w").close()
passed, failed = [], []


def say(line):
    with open(LOG, "a") as f:
        f.write(line + "\n")
    try:
        os.write(1, (line + "\n").encode())
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


class Recorder:
    """Writes down what a LocalRun says, in order."""

    def __init__(self, run):
        self.events = []
        run.text.connect(lambda t: self.events.append(("text", t)))
        run.tool.connect(lambda n, d: self.events.append(("tool", n, d)))
        run.tool_error.connect(lambda n: self.events.append(("tool_error", n)))
        run.finished.connect(lambda r: self.events.append(("finished", dict(r))))

    def done(self):
        return any(e[0] == "finished" for e in self.events)

    def result(self):
        return [e[1] for e in self.events if e[0] == "finished"][-1]

    def kinds(self):
        return [e[0] if e[0] != "tool" else "tool:" + e[1] for e in self.events]


def close_all():
    for d in list(App.listDocuments()):
        App.closeDocument(d)


def new_project(name):
    path = os.path.join(HOME, "projects", name)
    shutil.copytree(TEMPLATE, path)
    return path


def run_once(project, prompt, config, timeout=10):
    run = local_engine.LocalRun()
    rec = Recorder(run)
    run.start(project, prompt, config)
    pump(rec.done, timeout)
    return run, rec


def chat(project):
    with open(os.path.join(project, ".cadprompt", "local-chat.json")) as f:
        return json.load(f)["messages"]


# like a small model, the code does not make a document first
BOX = ('import threading\nTID = threading.get_ident()\n'
       'b = doc.addObject("Part::Box", "Plate")\nb.Length, b.Width, b.Height = 100, 50, 10')


def main():
    srv = FakeServer()
    cfg = {"url": srv.url, "model": "fake-model", "key": ""}
    main_thread = threading.get_ident()

    # ---- 1: tool call, answer, save ------------------------------------------
    say("--- 1 a request from start to end")
    close_all()
    project = new_project("plate")
    srv.add(call("run_python", {"code": BOX}, "c1", reasoning="I will make a box."),
            answer("The plate is ready.", reasoning="done"))
    run = local_engine.LocalRun()
    rec = Recorder(run)
    t0 = time.time()
    run.start(project, "Make a plate 100 x 50 x 10 mm.", cfg)
    check("start() returns at once", time.time() - t0 < 0.5 and run.running() and not rec.events,
          (time.time() - t0, rec.events))
    check("request finished", pump(rec.done, 10), rec.events)
    model = os.path.join(project, "model", "Model.FCStd")
    check("signals arrive in order: tool, text, save, finished",
          rec.kinds() == ["tool:run_python", "text", "tool:save", "finished"], rec.kinds())
    check("tool signal has name and a short detail", rec.events[0] == ("tool", "run_python", "import threading"),
          rec.events[0])
    check("text signal carries the answer", rec.events[1] == ("text", "The plate is ready."), rec.events[1])
    res = rec.result()
    check("finished: no error, result is the answer",
          res == {"is_error": False, "result": "The plate is ready."}, res)
    check("finished has no session_id", "session_id" not in res, res)
    check("running() is False afterwards", not run.running())
    check("the tool ran in the GUI main thread", local_tools._namespace.get("TID") == main_thread)
    doc = App.ActiveDocument
    check("the box exists", doc is not None and doc.getObject("Plate") is not None
          and abs(doc.Plate.Shape.Volume - 50000) < 1e-6)
    check("document saved to <project>/model/", os.path.isfile(model)
          and os.path.realpath(doc.FileName) == os.path.realpath(model), (doc.FileName, model))
    check("save is reported with its path", rec.events[2] == ("tool", "save", "model/Model.FCStd"), rec.events[2])
    check("one undo step for the run", doc.UndoMode == 1 and doc.UndoNames == ["CADprompt"], doc.UndoNames)
    check("local-chat.json exists", os.path.isfile(os.path.join(project, ".cadprompt", "local-chat.json")))
    check("stored roles", [m["role"] for m in chat(project)] == ["user", "assistant", "tool", "assistant"])
    check("no reasoning is stored", "reasoning" not in json.dumps(chat(project)))
    hist = local_engine.history(project)
    check("history() gives user, tool, claude",
          hist == [("user", "Make a plate 100 x 50 x 10 mm."), ("tool", "`run_python` import threading"),
                   ("claude", "The plate is ready.")], hist)
    first = srv.requests[0]
    check("no Authorization header without a key",
          "authorization" not in {k.lower() for k in first["headers"]})
    check("system prompt and four tools are sent",
          first["body"]["messages"][0]["role"] == "system"
          and "Mode: build" in first["body"]["messages"][0]["content"]
          and [t["function"]["name"] for t in first["body"]["tools"]]
          == ["run_python", "get_document_state", "read_project_file", "make_drawing"])
    check("the model saw the tool result",
          "+ new Plate: Part::Box, size 100.000 x 50.000 x 10.000" in srv.requests[1]["body"]["messages"][-1]["content"])

    # ---- 2: a second start continues, also after FreeCAD was closed -----------
    say("--- 2 the conversation continues")
    close_all()
    srv.add(answer("It is 100 mm long."))
    n = len(srv.requests)
    run2 = local_engine.LocalRun()
    rec2 = Recorder(run2)
    run2.start(project, "How long is it?", dict(cfg, key="sk-local-test"))
    check("with no document open, the project's model is opened",
          App.ActiveDocument is not None
          and os.path.realpath(App.ActiveDocument.FileName) == os.path.realpath(model))
    pump(rec2.done, 10)
    sent = srv.requests[n]["body"]["messages"]
    check("the server sees the earlier messages",
          [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
          and sent[1]["content"] == "Make a plate 100 x 50 x 10 mm."
          and sent[2]["tool_calls"][0]["function"]["name"] == "run_python"
          and sent[4]["content"] == "The plate is ready." and sent[5]["content"] == "How long is it?",
          [m["role"] for m in sent])
    check("Authorization header with a key",
          {k.lower(): v for k, v in srv.requests[n]["headers"].items()}.get("authorization") == "Bearer sk-local-test")
    check("a question alone does not save the document", rec2.kinds() == ["text", "finished"], rec2.kinds())
    check("history() has both requests",
          [k for k, _ in local_engine.history(project)] == ["user", "tool", "claude", "user", "claude"])
    check("history(limit) keeps the newest", local_engine.history(project, 2)
          == [("user", "How long is it?"), ("claude", "It is 100 mm long.")])

    # ---- 3: clear --------------------------------------------------------------
    say("--- 3 clear() starts a new chat")
    local_engine.clear(project)
    check("clear() empties the history", local_engine.history(project) == []
          and not os.path.exists(os.path.join(project, ".cadprompt", "local-chat.json")))
    check("clear() forgets the variables", "TID" not in local_tools._namespace)
    srv.add(answer("Hello."))
    n = len(srv.requests)
    run_once(project, "Hello", cfg)
    check("after clear() the server gets a fresh conversation",
          [m["role"] for m in srv.requests[n]["body"]["messages"]] == ["system", "user"])
    local_engine.clear(project)
    local_engine.clear(project)            # twice is harmless

    # ---- 4: slash commands -----------------------------------------------------
    say("--- 4 slash commands from the project template")
    fake = types.ModuleType("cadprompt.drawing")      # the real drawing module has its own tests
    drawn = []
    fake.make_drawing = lambda project, sheet="A3", obj_name="": (
        drawn.append((project, sheet, obj_name)) or "Drawing made: drawings/Plate.pdf, scale 1:1. Missing: tolerances.")
    sys.modules["cadprompt.drawing"] = fake
    srv.add(answer("Teach mode: one step at a time. What are we building?"))
    n = len(srv.requests)
    run_once(project, "/teach", cfg)
    body = srv.requests[n]["body"]["messages"]
    check("/teach writes the mode file", projects.get_mode(project) == "teach")
    check("/teach is expanded from teach.md without the front matter",
          body[-1]["content"].startswith("Write `teach` into `.cadprompt/mode`, then work in teach mode")
          and "description:" not in body[-1]["content"] and "---" not in body[-1]["content"]
          and "$ARGUMENTS" not in body[-1]["content"], body[-1]["content"])
    check("the system prompt is in teach mode", "Mode: teach" in body[0]["content"]
          and "Mode: build" not in body[0]["content"])
    check("history() shows the command as typed", local_engine.history(project)[0] == ("user", "/teach"))

    srv.add(call("make_drawing", {"sheet": "A4"}, "d1"), answer("The drawing is in drawings/Plate.pdf."))
    n = len(srv.requests)
    _, rec4 = run_once(project, "/drawing A4 with a hole table", cfg)
    text = srv.requests[n]["body"]["messages"][-1]["content"]
    check("/drawing puts the arguments in place of $ARGUMENTS",
          "Sheet size and wishes: A4 with a hole table" in text and "$ARGUMENTS" not in text
          and "argument-hint" not in text and text.startswith("Make a workshop drawing"), text)
    check("make_drawing reaches drawing.make_drawing", drawn == [(project, "A4", "")], drawn)
    check("make_drawing is shown as a tool", rec4.events[0] == ("tool", "make_drawing", "A4"), rec4.events[:1])
    check("the model gets the drawing report",
          srv.requests[n + 1]["body"]["messages"][-1]["content"].startswith("Drawing made: drawings/Plate.pdf"))
    del sys.modules["cadprompt.drawing"]
    import cadprompt
    if hasattr(cadprompt, "drawing"):
        del cadprompt.drawing

    srv.add(answer("Build mode."), answer("I do not know that command."))
    n = len(srv.requests)
    run_once(project, "/build a bracket", cfg)
    check("/build writes the mode file and keeps the arguments", projects.get_mode(project) == "build"
          and "a bracket" in srv.requests[n]["body"]["messages"][-1]["content"]
          and "Mode: build" in srv.requests[n]["body"]["messages"][0]["content"])
    run_once(project, "/nosuch thing", cfg)
    check("an unknown command is sent as plain text",
          srv.requests[n + 1]["body"]["messages"][-1]["content"] == "/nosuch thing")
    check("expand() leaves normal text alone",
          local_engine.expand(project, "Make it 5/8 inch wide") == ("Make it 5/8 inch wide", None))

    # ---- 5: tool error, automatic check ----------------------------------------
    say("--- 5 a tool error and the automatic check")
    local_engine.clear(project)
    srv.add(call("run_python", {"code": 'x = doc.addObject("Part::Box", "Half")\nx.Length = 1 / 0'}, "e1"),
            answer("Done."),
            call("run_python", {"code": 'x = doc.addObject("Part::Box", "Block")'}, "e2"),
            answer("Now the block is there."))
    _, rec5 = run_once(project, "Add a block.", cfg)
    check("tool_error and the automatic check arrive in order",
          rec5.kinds() == ["tool:run_python", "tool_error", "text", "text", "tool:run_python", "text",
                           "tool:save", "finished"], rec5.kinds())
    check("the check is shown as a note", rec5.events[3][1].startswith("*Automatic check"), rec5.events[3])
    doc = App.ActiveDocument
    check("the failed run left nothing behind", doc.getObject("Half") is None and doc.getObject("Block") is not None)
    check("the automatic check is not shown as a user message",
          [k for k, _ in local_engine.history(project)] == ["user", "tool", "claude", "tool", "claude"],
          local_engine.history(project))

    # ---- 6: errors ---------------------------------------------------------------
    say("--- 6 errors end cleanly")
    before = chat(project)
    mtime = os.path.getmtime(model)
    srv.add(error(401, "Incorrect API key"))
    run6, rec6 = run_once(project, "Hello?", cfg)
    res = rec6.result()
    check("HTTP 401 ends with a clear error", res.get("is_error") is True and "API key" in res["result"]
          and "session_id" not in res and rec6.kinds() == ["finished"] and not run6.running(), res)
    check("a failed request is not kept and nothing is saved",
          chat(project) == before and os.path.getmtime(model) == mtime)
    _, rec6b = run_once(project, "Hello?", {"url": "http://127.0.0.1:9/v1", "model": "m", "key": ""})
    check("a server that is not there ends with a clear error",
          rec6b.result().get("is_error") is True and "does not answer" in rec6b.result()["result"], rec6b.result())
    _, rec6c = run_once(project, "Hello?", {"url": srv.url, "model": "", "key": ""})
    check("no model set ends with a clear error",
          rec6c.result().get("is_error") is True and "No model is set" in rec6c.result()["result"])

    # ---- 7: a new unsaved document never overwrites a model ---------------------
    say("--- 7 saving a new document")
    close_all()
    App.newDocument("Model")
    local_engine.clear(project)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Cylinder", "Pin")'}, "s1"), answer("A pin."))
    _, rec7 = run_once(project, "Make a pin.", cfg)
    check("a second document gets its own file", ("tool", "save", "model/Model-2.FCStd") in rec7.events
          and os.path.isfile(os.path.join(project, "model", "Model-2.FCStd"))
          and os.path.getmtime(model) == mtime, rec7.events)

    # ---- 8: stop during a slow reply ----------------------------------------------
    say("--- 8 stop() while the server is slow")
    ticks = []
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: ticks.append(time.time()))
    timer.start(10)
    local_engine.clear(project)
    srv.add(slow(6, answer("LATE ANSWER")))
    run8 = local_engine.LocalRun()
    rec8 = Recorder(run8)
    run8.start(project, "Take your time.", cfg)
    t0 = len(ticks)
    pump(timeout=0.6)
    check("the GUI stays responsive while the model works", len(ticks) - t0 >= 20 and run8.running(),
          len(ticks) - t0)
    t = time.time()
    run8.stop()
    took = time.time() - t
    check("stop() returns at once", took < 0.2, took)
    check("finished comes once, with stopped=True",
          rec8.events == [("finished", {"is_error": False, "result": "", "stopped": True})], rec8.events)
    check("running() is False after stop()", not run8.running())
    t1 = len(ticks)
    check("the worker thread ends soon after stop()",
          pump(lambda: all(w.isFinished() for w in local_engine._threads), 2.0))
    pump(timeout=0.5)
    check("the event loop is alive after stop()", len(ticks) - t1 >= 10, len(ticks) - t1)
    check("nothing more is emitted after stop()", len(rec8.events) == 1, rec8.events)
    check("the waiting request was closed", srv.dropped == 1, srv.dropped)
    stored = chat(project)
    check("the stopped request is stored as stopped",
          [m["content"] for m in stored] == ["Take your time.", "*Stopped.*"], stored)
    run8.stop()
    check("a second stop() does nothing", len(rec8.events) == 1)
    srv.add(answer("Here again."))
    rec8.events.clear()
    run8.start(project, "And now?", cfg)
    pump(rec8.done, 10)
    check("the same LocalRun works again after stop()",
          rec8.events == [("text", "Here again."), ("finished", {"is_error": False, "result": "Here again."})],
          rec8.events)

    # a slow reply whose late answer does arrive: it must not be emitted or saved
    say("--- 9 the late result of a stopped run")
    local_engine.clear(project)

    class Deaf(local_agent.Agent):       # an agent that cannot be interrupted while it waits
        def stop(self):
            self._stop.set()
    real_agent = local_agent.Agent
    local_agent.Agent = Deaf
    try:
        srv.add(slow(0.8, call("run_python", {"code": 'doc.addObject("Part::Box", "Late")'}, "l1", text="LATE TEXT")))
        n, dropped = len(srv.requests), srv.dropped
        run9 = local_engine.LocalRun()
        rec9 = Recorder(run9)
        run9.start(project, "Slow one.", cfg)
        pump(timeout=0.3)
        run9.stop()
        pump(lambda: all(w.isFinished() for w in local_engine._threads), 3.0)
        pump(timeout=0.3)
    finally:
        local_agent.Agent = real_agent
    check("the late reply did arrive at the worker", len(srv.requests) == n + 1 and srv.dropped == dropped,
          (len(srv.requests) - n, srv.dropped - dropped))
    check("a late result is never emitted", rec9.events == [("finished", {"is_error": False, "result": "", "stopped": True})],
          rec9.events)
    check("a late tool call is never run", App.ActiveDocument.getObject("Late") is None)
    check("a late result is never saved", "LATE" not in json.dumps(chat(project))
          and [m["content"] for m in chat(project)] == ["Slow one.", "*Stopped.*"], chat(project))

    # ---- 10: stop from inside a tool: the worker waits for the main thread -------
    say("--- 10 stop() while the worker waits for a tool")
    local_engine.clear(project)
    run10 = local_engine.LocalRun()
    rec10 = Recorder(run10)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "DuringStop")\nSTOP()'}, "w1"))
    n = len(srv.requests)
    run10.start(project, "Stop in the middle.", cfg)
    local_tools._namespace["STOP"] = run10.stop
    check("no deadlock", pump(rec10.done, 5), rec10.events)
    check("the worker thread ends", pump(lambda: all(w.isFinished() for w in local_engine._threads), 2.0))
    pump(timeout=0.3)
    check("finished once with stopped=True, no further request to the server",
          rec10.kinds() == ["tool:run_python", "finished"] and rec10.result().get("stopped") is True
          and len(srv.requests) == n + 1, (rec10.kinds(), len(srv.requests) - n))

    # ---- 11: the panel is closed in the middle of a run -------------------------
    say("--- 11 the LocalRun is destroyed during a run")
    local_engine.clear(project)
    parent = QtCore.QObject()
    run11 = local_engine.LocalRun(parent)
    dropped = srv.dropped
    srv.add(slow(6, answer("NOBODY LISTENS")))
    run11.start(project, "Bye.", cfg)
    pump(timeout=0.3)
    import shiboken6
    shiboken6.delete(parent)
    t1 = len(ticks)
    check("its worker ends by itself",
          pump(lambda: all(w.isFinished() for w in local_engine._threads), 2.0)
          and not shiboken6.isValid(run11))
    pump(timeout=0.3)
    check("its request was closed and the event loop is alive", srv.dropped == dropped + 1 and len(ticks) - t1 >= 5,
          (srv.dropped - dropped, len(ticks) - t1))

    # a worker that waits for a tool when its LocalRun dies must not wait for ever
    parent = QtCore.QObject()
    run12 = local_engine.LocalRun(parent)
    srv.add(call("run_python", {"code": "NEVER_RUN = 1"}, "g1"))
    n = len(srv.requests)
    run12.start(project, "Bye again.", cfg)
    end = time.time() + 5
    while len(srv.requests) == n and time.time() < end:
        time.sleep(0.01)                 # no event processing: the tool request stays in the queue
    time.sleep(0.3)
    shiboken6.delete(parent)
    check("a worker that waits for a tool ends when its LocalRun is destroyed",
          pump(lambda: all(w.isFinished() for w in local_engine._threads), 2.0)
          and len(srv.requests) == n + 1 and "NEVER_RUN" not in local_tools._namespace)

    timer.stop()
    check("the fake server's script was used exactly", srv.overrun == 0 and not srv.script,
          (srv.overrun, len(srv.script)))
    local_engine._shutdown()
    check("no worker thread is left", not local_engine._threads, len(local_engine._threads))
    srv.close()


def finish():
    code = 1
    try:
        main()
        code = 0 if not failed else 1
    except Exception:
        say("EXCEPTION in the test itself:\n" + traceback.format_exc())
    say("")
    say("%d passed, %d failed%s" % (len(passed), len(failed), "" if not failed else ": " + "; ".join(failed)))
    say("RESULT: " + ("OK" if code == 0 else "FAILED"))
    os._exit(code)


def watchdog():
    say("WATCHDOG: the test hung for 240 s")
    os._exit(3)


threading.Timer(240, watchdog).start()
QtCore.QTimer.singleShot(500, finish)
