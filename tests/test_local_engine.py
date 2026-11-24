# RUN: freecad-gui
"""End-to-end test of cadprompt/local_engine.py in a FreeCAD GUI without a display:

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/test_local_engine.py

A fake OpenAI server plays the model, so the result does not depend on any AI.
The report goes to stdout and to <FREECAD_USER_HOME>/test_local_engine.log.
FREECAD_USER_HOME keeps the test away from the user's real FreeCAD settings.
"""

import base64
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import traceback
import types
import zlib

if not os.environ.get("FREECAD_USER_HOME"):
    os.write(1, b"Set FREECAD_USER_HOME to a fresh folder first.\n")
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
ADDON = os.path.dirname(HERE)
TEMPLATE = os.path.join(ADDON, "template")
sys.path.insert(0, ADDON)
sys.path.insert(0, HERE)

import FreeCAD as App                                   # noqa: E402
from PySide import QtCore, QtGui, QtWidgets             # noqa: E402

# Without a display Qt warns "This plugin does not support raise()" when FreeCAD
# shows a notification, and FreeCAD 1.1 deadlocks while it reports that warning
# (NotificationArea locks its mutex twice). Mute the warning: a test-only problem.
QtCore.QLoggingCategory.setFilterRules("default.warning=false")

from cadprompt import backends, local_agent, local_engine, local_tools, projects    # noqa: E402
from local_fake_server import FakeAnthropic, FakeServer, answer, call, claude, cut, error, slow   # noqa: E402
import user_yes                                                                               # noqa: E402
import windows_paths                                                                          # noqa: E402

HOME = os.environ["FREECAD_USER_HOME"]
# This file tests the engine, with no panel that could ask: the user of this scratch profile
# turned "Run the AI's code without asking" on. Part 16 turns it off and tests the question.
user_yes.run_without_asking()
LOG = os.path.join(HOME, "test_local_engine.log")
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


def no_backup(kinds):
    """The signals without the backup line: whether one comes depends on the file, not on the run."""
    return [k for k in kinds if k != "tool:backup"]


def copies_of(project):
    """The file names in model/.backup of a project."""
    folder = os.path.join(project, "model", ".backup")
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def close_all():
    for d in list(App.listDocuments()):
        App.closeDocument(d)


def new_project(name):
    path = os.path.join(HOME, "projects", name)
    shutil.copytree(TEMPLATE, path)
    return path


def run_once(project, prompt, config, timeout=10, images=None):
    run = local_engine.LocalRun()
    rec = Recorder(run)
    run.start(project, prompt, config, images=images)
    pump(rec.done, timeout)
    return run, rec


def chat(project):
    with open(os.path.join(project, ".cadprompt", "local-chat.json"), encoding="utf-8") as f:
        return json.load(f)["messages"]


def picture(path, width, height, kind):
    """A test image file: grey, with a dark bar, so it is not one flat colour."""
    img = QtGui.QImage(width, height, QtGui.QImage.Format_RGB32)
    img.fill(QtGui.QColor(200, 200, 200))
    for x in range(width // 4, width // 2):
        for y in range(0, height, 7):
            img.setPixelColor(x, y, QtGui.QColor(20, 20, 20))
    return img.save(path, kind)


def sent_image(data):
    """(width, height) of an image as it was sent, from its base64 text."""
    img = QtGui.QImage.fromData(base64.b64decode(data))
    return img.width(), img.height()


def same_pixels(a, b):
    a, b = (i.convertToFormat(QtGui.QImage.Format_ARGB32) for i in (a, b))
    return a.size() == b.size() and all(a.pixel(x, y) == b.pixel(x, y)
                                        for x in range(a.width()) for y in range(a.height()))


def opened(fn):
    """Run fn() and give (its error text or "", the paths that os.path.realpath was asked for)."""
    seen, real, said = [], os.path.realpath, ""
    os.path.realpath = lambda path, *a, **kw: seen.append(str(path)) or real(path, *a, **kw)
    try:
        fn()
    except local_engine.ImageError as exc:
        said = str(exc)
    finally:
        os.path.realpath = real
    return said, seen


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
    check("system prompt and the six tools are sent",
          first["body"]["messages"][0]["role"] == "system"
          and "Mode: build" in first["body"]["messages"][0]["content"]
          and [t["function"]["name"] for t in first["body"]["tools"]]
          == ["run_python", "get_document_state", "read_project_file", "make_drawing", "find_joints", "make_weld"])
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
          body[-1]["content"].startswith("The mode is `teach` now.") and "Work in teach mode" in body[-1]["content"]
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
          no_backup(rec5.kinds()) == ["tool:run_python", "tool_error", "text", "text", "tool:run_python",
                                      "text", "tool:save", "finished"], rec5.kinds())
    notes = [e[1] for e in rec5.events if e[0] == "text"]
    check("the check is shown as a note", notes[1].startswith("*Automatic check"), notes)
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

    # ---- 7a: the run belongs to one document ---------------------------------------
    say("--- 7a the run stays in its document when the user clicks another one")
    close_all()
    two = new_project("two-docs")
    file_a = os.path.join(two, "model", "A.FCStd")
    file_b = os.path.join(HOME, "other-work", "B.FCStd")
    os.makedirs(os.path.dirname(file_b))
    doc_a = App.newDocument("A")
    doc_a.addObject("Part::Box", "Abox")
    doc_a.recompute()
    doc_a.saveAs(file_a)
    doc_b = App.newDocument("B")
    doc_b.addObject("Part::Box", "Bbox")
    doc_b.recompute()
    doc_b.saveAs(file_b)
    doc_b.Bbox.Length = 77                       # the user's experiment, not saved
    App.setActiveDocument("A")
    size_a, mtime_b = os.path.getsize(file_a), os.path.getmtime(file_b)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Cylinder", "Hole")'}, "a1"),
            call("run_python", {"code": 'doc.addObject("Part::Sphere", "Second")'}, "a2"),
            answer("The hole is there."))
    run7a = local_engine.LocalRun()
    rec7a = Recorder(run7a)
    # the GUI is free while the model thinks: the user clicks the tab of B before each tool and before the end
    run7a.tool.connect(lambda name, detail: App.setActiveDocument("B") if name == "run_python" else None)
    run7a.text.connect(lambda text: App.setActiveDocument("B"))
    run7a.start(two, "Add a hole.", cfg)
    check("the request ends", pump(rec7a.done, 10), rec7a.events)
    check("both tool calls worked in the run's document",
          [o.Name for o in doc_a.Objects] == ["Abox", "Hole", "Second"]
          and [o.Name for o in doc_b.Objects] == ["Bbox"],
          ([o.Name for o in doc_a.Objects], [o.Name for o in doc_b.Objects]))
    check("the run's document is saved, by name", ("tool", "save", "model/A.FCStd") in rec7a.events
          and os.path.getsize(file_a) != size_a, rec7a.events)
    check("the other document is not saved: not over its file, not into the project",
          os.path.getmtime(file_b) == mtime_b
          and not [f for f in os.listdir(os.path.join(two, "model")) if f.startswith("B")],
          os.listdir(os.path.join(two, "model")))
    copies = copies_of(two)
    check("before the first change the model file was copied to model/.backup",
          len(copies) == 1 and re.fullmatch(r"A-\d{8}-\d{6}\.FCStd", copies[0])
          and os.path.getsize(os.path.join(two, "model", ".backup", copies[0])) == size_a
          and rec7a.kinds().count("tool:backup") == 1
          and ("tool", "backup", "model/.backup/" + copies[0]) in rec7a.events, (copies, rec7a.events))

    # ---- 7b: a request that does not end well is not saved ---------------------------
    say("--- 7b the step limit, a problem that stays, an error: the model file is not touched")
    App.setActiveDocument("A")
    time.sleep(1.1)                              # the next backup copy gets another name
    mtime_a = os.path.getmtime(file_a)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Extra1")'}, "n1"),
            call("run_python", {"code": 'doc.addObject("Part::Box", "Extra2")'}, "n2"))
    _, rec7b = run_once(two, "Go on.", dict(cfg, max_steps=2))
    res = rec7b.result()
    check("the step limit ends the run as incomplete",
          res.get("is_error") is False and res.get("incomplete") is True and "step limit (2)" in res["result"], res)
    check("at the step limit the model file is not saved, and the user is told",
          os.path.getmtime(file_a) == mtime_a and "tool:save" not in rec7b.kinds()
          and ("text", local_engine.NOT_SAVED) in rec7b.events and doc_a.getObject("Extra2") is not None,
          rec7b.events)
    check("the file had changed since the last copy: one more backup",
          len(copies_of(two)) == 2, copies_of(two))
    srv.add(call("run_python", {"code": "1 / 0"}, "n3"), answer("Done."), answer("Done."), answer("It is done."))
    _, rec7c = run_once(two, "And more.", cfg)
    check("a problem that the model did not repair: not saved",
          rec7c.result().get("incomplete") is True and "still shows a problem" in rec7c.result()["result"]
          and os.path.getmtime(file_a) == mtime_a and "tool:save" not in rec7c.kinds()
          and ("text", local_engine.NOT_SAVED) in rec7c.events, rec7c.events)
    check("the file did not change: no new backup copy",
          len(copies_of(two)) == 2 and "tool:backup" not in rec7c.kinds(), copies_of(two))
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Extra3")'}, "n4"), error(500, "GPU ran out of memory"))
    _, rec7d = run_once(two, "And more.", cfg)
    check("an error after a change: not saved, and the user is told",
          rec7d.result().get("is_error") is True and os.path.getmtime(file_a) == mtime_a
          and "tool:save" not in rec7d.kinds() and ("text", local_engine.NOT_SAVED) in rec7d.events, rec7d.events)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Extra3b")'}, "n4b"),
            cut(answer("Now I add the four ho")))
    _, rec7d2 = run_once(two, "And more.", cfg)
    check("an answer that the server cut off at the token limit: not saved, and the user is told",
          rec7d2.result().get("is_error") is False and rec7d2.result().get("incomplete") is True
          and "cut off" in rec7d2.result()["result"] and os.path.getmtime(file_a) == mtime_a
          and "tool:save" not in rec7d2.kinds() and ("text", local_engine.NOT_SAVED) in rec7d2.events
          and doc_a.getObject("Extra3b") is not None, rec7d2.events)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Extra4")'}, "n5"), answer("Good now."))
    _, rec7e = run_once(two, "Once more.", cfg)
    check("the next request that ends well is saved again",
          rec7e.result() == {"is_error": False, "result": "Good now."} and os.path.getmtime(file_a) != mtime_a
          and ("tool", "save", "model/A.FCStd") in rec7e.events
          and not [e for e in rec7e.events if e == ("text", local_engine.NOT_SAVED)], rec7e.events)

    say("--- 7c backup copies: the newest 10 of each model")
    folder = os.path.join(two, "model", ".backup")
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))
    for i in range(12):
        open(os.path.join(folder, "A-202601%02d-120000.FCStd" % (i + 1)), "w").close()
    for name in ("A-2-20260101-120000.FCStd", "Axle-20260101-120000.FCStd", "A-notes.FCStd"):
        open(os.path.join(folder, name), "w").close()
    copy = local_engine.backup(two, file_a)
    left = sorted(os.listdir(folder))
    mine = [f for f in left if re.fullmatch(r"A-\d{8}-\d{6}\.FCStd", f)]
    check("backup() keeps the newest 10 copies of this model",
          copy is not None and len(mine) == 10 and os.path.basename(copy) == mine[-1]
          and mine[0] == "A-20260104-120000.FCStd", mine)
    check("backup() leaves the copies of other models alone",
          all(f in left for f in ("A-2-20260101-120000.FCStd", "Axle-20260101-120000.FCStd", "A-notes.FCStd")), left)
    check("backup() makes no second copy of a file that was not saved since",
          local_engine.backup(two, file_a) is None and sorted(os.listdir(folder)) == left)
    check("backup() of a file that is not there does nothing",
          local_engine.backup(two, os.path.join(two, "model", "none.FCStd")) is None)

    # ---- 7d: Windows, the document on another drive -----------------------------------
    say("--- 7d the end of a run always arrives")
    real_relpath = os.path.relpath

    def other_drive(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")
    os.path.relpath = other_drive                # what ntpath.relpath does for D:\\x and C:\\project
    try:
        srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Drive")'}, "w1"), answer("Saved."))
        run7f, rec7f = run_once(two, "One more box.", cfg)
    finally:
        os.path.relpath = real_relpath
    check("a document on another drive: saved, shown by its file name, and finished arrives",
          rec7f.done() and ("tool", "save", "A.FCStd") in rec7f.events and not run7f.running(), rec7f.events)
    run7g = local_engine.LocalRun()
    rec7g = Recorder(run7g)
    run7g._save_document = lambda: [][0]         # any fault in the save step
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Fault")'}, "w2"), answer("Done."))
    run7g.start(two, "Another box.", cfg)
    check("finished arrives also when the save step fails", pump(rec7g.done, 10) and not run7g.running(),
          rec7g.events)

    # ---- 7e: the user closes the run's document ----------------------------------------
    say("--- 7e the run's document is closed during the run")
    App.setActiveDocument("A")
    mtime_b = os.path.getmtime(file_b)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "IntoA")'}, "k1"),
            call("run_python", {"code": 'doc.addObject("Part::Box", "IntoB")'}, "k2"),
            call("get_document_state", {}, "k3"), answer("The document is gone."), answer("It is gone."),
            answer("It is gone."))
    run7h = local_engine.LocalRun()
    rec7h = Recorder(run7h)
    seen = []

    def close_a(name, detail):
        seen.append(name)
        if seen.count("run_python") == 2 and "A" in App.listDocuments():
            App.closeDocument("A")               # B is the active document now
    run7h.tool.connect(close_a)
    n = len(srv.requests)
    run7h.start(two, "Two boxes.", cfg)
    check("the request ends", pump(rec7h.done, 10), rec7h.events)
    told = [m["content"] for m in srv.requests[n + 2]["body"]["messages"] if m["role"] == "tool"]
    check("the model is told that the document was closed",
          told[-1] == local_engine.DOC_CLOSED % "A"
          and srv.requests[n + 3]["body"]["messages"][-1]["content"] == local_engine.DOC_CLOSED % "A", told)
    check("no tool works in the other document, and it is not saved",
          [o.Name for o in doc_b.Objects] == ["Bbox"] and os.path.getmtime(file_b) == mtime_b
          and "tool:save" not in rec7h.kinds(), ([o.Name for o in doc_b.Objects], rec7h.kinds()))
    close_all()

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
    # 30 ticks are possible in 0.6 s; a slow test machine (macOS on GitHub) reaches 18
    check("the GUI stays responsive while the model works", len(ticks) - t0 >= 10 and run8.running(),
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
          no_backup(rec10.kinds()) == ["tool:run_python", "finished"] and rec10.result().get("stopped") is True
          and len(srv.requests) == n + 1, (rec10.kinds(), len(srv.requests) - n))

    # ---- 10b: stop immediately after a tool ----------------------------------------
    say("--- 10b stop() in the moment after a tool: the tool step is not lost")
    local_engine.clear(project)
    run10b = local_engine.LocalRun()
    rec10b = Recorder(run10b)

    def stop_after_the_tool():
        if run10b._in_tool:                      # FreeCAD's view fit lets timers through: wait for the end
            QtCore.QTimer.singleShot(0, stop_after_the_tool)
        else:
            run10b.stop()
    real_tool = local_engine._Worker._tool

    def late_report(worker, name, args):         # the worker is slow to report, as it is after a real click
        result = real_tool(worker, name, args)
        time.sleep(0.4)
        return result
    local_engine._Worker._tool = late_report
    try:
        srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "JustMade")\nARM()'}, "j1"))
        run10b.start(project, "Make a box.", cfg)
        local_tools._namespace["ARM"] = lambda: QtCore.QTimer.singleShot(0, stop_after_the_tool)
        check("stopped", pump(rec10b.done, 5) and rec10b.result().get("stopped") is True, rec10b.events)
        pump(lambda: all(w.isFinished() for w in local_engine._threads), 3.0)
        pump(timeout=0.2)
    finally:
        local_engine._Worker._tool = real_tool
    stored = chat(project)
    check("the tool did run", App.ActiveDocument.getObject("JustMade") is not None)
    check("the stored conversation has the tool step before '*Stopped.*'",
          [m["role"] for m in stored] == ["user", "assistant", "tool", "assistant"]
          and stored[1]["tool_calls"][0]["function"]["name"] == "run_python"
          and "JustMade" in stored[1]["tool_calls"][0]["function"]["arguments"]
          and stored[2]["tool_call_id"] == stored[1]["tool_calls"][0]["id"]
          and "+ new JustMade: Part::Box" in stored[2]["content"] and stored[3]["content"] == "*Stopped.*",
          stored)
    srv.add(answer("The box is there already."))
    n = len(srv.requests)
    run_once(project, "Go on.", cfg)
    check("the next request tells the model what was done",
          [m["role"] for m in srv.requests[n]["body"]["messages"]]
          == ["system", "user", "assistant", "tool", "assistant", "user"],
          [m["role"] for m in srv.requests[n]["body"]["messages"]])

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
    srv.add(slow(0.3, call("run_python", {"code": "NEVER_RUN = 1"}, "g1")))
    n = len(srv.requests)
    run12.start(project, "Bye again.", cfg)
    # the main thread lets the request go out (the gate), then processes no event:
    # the answer comes 0.3 s later, and the tool request stays in the queue
    pump(lambda: len(srv.requests) > n, 5)
    time.sleep(0.8)
    shiboken6.delete(parent)
    check("a worker that waits for a tool ends when its LocalRun is destroyed",
          pump(lambda: all(w.isFinished() for w in local_engine._threads), 2.0)
          and len(srv.requests) == n + 1 and "NEVER_RUN" not in local_tools._namespace)

    # ---- 12: an Anthropic API key: the same engine, the other wire format ---------
    say("--- 12 the Anthropic Messages API through the whole engine")
    close_all()
    local_tools.reset()
    cl = FakeAnthropic()
    acfg = {"kind": "anthropic", "url": cl.url, "model": "claude-fake-1", "key": "sk-ant-made-up"}
    cfg12 = dict(cfg, kind="openai", key="LOCAL-KEY-made-up")
    project12 = new_project("anthropic")
    cl.add(claude("I make the plate.", [("run_python", {"code": BOX}, "toolu_1")], thinking="one"),
           claude("A plate 100 x 50 x 10.", thinking="two"))
    run, rec = run_once(project12, "Make a plate.", acfg)
    doc = App.ActiveDocument
    check("anthropic: the tool call built the plate, the answer arrived, the model was saved",
          rec.done() and not rec.result()["is_error"] and rec.result()["result"] == "A plate 100 x 50 x 10."
          and doc is not None and doc.getObject("Plate") is not None and bool(doc.FileName)
          and no_backup(rec.kinds()) == ["text", "tool:run_python", "text", "tool:save", "finished"],
          (rec.events, doc and doc.FileName))
    first, second = cl.bodies()[-2:]
    head = {k.lower(): v for k, v in cl.requests[-1]["headers"].items()}
    check("anthropic: POST /v1/messages with x-api-key and the API version, no Authorization header",
          cl.requests[-1]["path"] == "/v1/messages" and head.get("x-api-key") == "sk-ant-made-up"
          and head.get("anthropic-version") == "2026-10-21" and "authorization" not in head, head)
    check("anthropic: the system prompt is a field of its own, the six tools are in Anthropic's shape",
          first["system"].startswith("You are CADprompt") and [t["name"] for t in first["tools"]] ==
          ["run_python", "get_document_state", "read_project_file", "make_drawing", "find_joints", "make_weld"]
          and all(set(t) == {"name", "description", "input_schema"} for t in first["tools"])
          and first["max_tokens"] > 0 and "temperature" not in first, str(first)[:300])
    check("anthropic: the tool result goes back as a tool_result, the thinking block unchanged before the call",
          [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
          and [b["type"] for b in second["messages"][1]["content"]] == ["thinking", "text", "tool_use"]
          and second["messages"][1]["content"][0]["signature"] == "SIGNATURE-one"
          and second["messages"][2]["content"][0]["type"] == "tool_result"
          and second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1", str(second["messages"])[:600])
    stored = chat(project12)
    check("anthropic: the stored conversation is the neutral one, without any thinking",
          [m["role"] for m in stored] == ["user", "assistant", "tool", "assistant"]
          and stored[1]["tool_calls"][0]["function"]["name"] == "run_python"
          and stored[2]["tool_call_id"] == "toolu_1" and "SIGNATURE" not in json.dumps(stored)
          and "_native" not in json.dumps(stored) and "sk-ant" not in json.dumps(stored), str(stored)[:400])

    # the source changes in the middle of the project, and back
    n = len(srv.requests)
    srv.add(answer("It is a plate."))
    run, rec = run_once(project12, "What did you make?", cfg12)
    body = srv.requests[-1]["body"]["messages"]
    check("the same conversation goes on with a local server (OpenAI format)",
          rec.done() and not rec.result()["is_error"] and len(srv.requests) == n + 1
          and [m["role"] for m in body] == ["system", "user", "assistant", "tool", "assistant", "user"]
          and body[2]["tool_calls"][0]["id"] == "toolu_1" and body[3]["tool_call_id"] == "toolu_1"
          and "thinking" not in json.dumps(body), [m["role"] for m in body])
    cl.add(claude("Yes, a plate."))
    run, rec = run_once(project12, "Sure?", acfg)
    body = cl.bodies()[-1]["messages"]
    check("... and goes on with Anthropic again",
          rec.done() and rec.result()["result"] == "Yes, a plate."
          and [m["role"] for m in body] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
          and body[5]["content"] == [{"type": "text", "text": "It is a plate."}], [m["role"] for m in body])
    local_wire, remote_wire = json.dumps(srv.requests[n:]), json.dumps(cl.requests)
    check("each key went to its own service only",
          "Bearer LOCAL-KEY-made-up" in local_wire and "sk-ant" not in local_wire
          and "x-api-key" not in local_wire.lower() and "sk-ant-made-up" in remote_wire
          and "LOCAL-KEY" not in remote_wire and "authorization" not in remote_wire.lower())
    cl.add(("error", 401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    run, rec = run_once(project12, "Once more.", acfg)
    check("anthropic: a refused key is a plain error without 'local model' and without the key",
          rec.result()["is_error"] and "Anthropic did not accept the API key" in rec.result()["result"]
          and "local" not in rec.result()["result"] and "sk-ant" not in rec.result()["result"], rec.result())

    # ---- 13: images ------------------------------------------------------------------
    say("--- 13 images go with a message: sent small, stored as a path")
    jpeg = any(bytes(f.data()).lower() == b"jpeg" for f in QtGui.QImageWriter.supportedImageFormats())
    photo = os.path.join(HOME, "big photo.jpg" if jpeg else "big photo.png")
    os.makedirs(os.path.join(project12, "refs"), exist_ok=True)
    part = os.path.join(project12, "refs", "part.png")
    check("(setup) the two test images are written",
          picture(photo, 3136, 1000, "JPEG" if jpeg else "PNG") and picture(part, 40, 30, "PNG"))
    cl.add(claude("A grey plate with a dark bar."))
    run, rec = run_once(project12, "What is this?", acfg, images=[photo, "refs/part.png"])
    last = cl.bodies()[-1]["messages"][-1]["content"]
    blocks = [b for b in last if b["type"] == "image"]
    check("anthropic: two image blocks before the text of the message",
          rec.done() and not rec.result()["is_error"] and [b["type"] for b in last] == ["image", "image", "text"]
          and last[2]["text"] == "What is this?" and all(b["source"]["type"] == "base64" for b in blocks),
          [b["type"] for b in last])
    check("a large photo is sent with its longest side at 1568 px" + (", as JPEG" if jpeg else ""),
          len(blocks) == 2 and sent_image(blocks[0]["source"]["data"]) == (1568, 500)
          and blocks[0]["source"]["media_type"] == ("image/jpeg" if jpeg else "image/png"),
          len(blocks) == 2 and (sent_image(blocks[0]["source"]["data"]), blocks[0]["source"]["media_type"]))
    check("a small PNG is sent as it is, as PNG",
          len(blocks) == 2 and sent_image(blocks[1]["source"]["data"]) == (40, 30)
          and blocks[1]["source"]["media_type"] == "image/png")
    stored = chat(project12)
    asked = [m for m in stored if m.get("images")]
    copied = asked[0]["images"][0] if asked else ""
    check("the stored conversation has the paths in the project, never the image data",
          len(asked) == 1 and asked[0]["content"] == "What is this?" and asked[0]["images"][1] == "refs/part.png"
          and copied.startswith("refs/") and copied.endswith(os.path.basename(photo))
          and os.path.isfile(os.path.join(project12, *copied.split("/")))
          and blocks[0]["source"]["data"][:60] not in json.dumps(stored) and len(json.dumps(stored)) < 20000,
          str(asked)[:300])
    check("the history shows the note 'with 2 images' under the message",
          [e for e in local_engine.history(project12) if e[1] in ("What is this?", "with 2 images")] ==
          [("user", "What is this?"), ("note", "with 2 images")], local_engine.history(project12)[-4:])
    srv.add(answer("Still the plate."))
    run, rec = run_once(project12, "And now?", cfg12)
    seen = [m for m in srv.requests[-1]["body"]["messages"] if isinstance(m.get("content"), list)]
    urls = [c["image_url"]["url"] for c in seen[0]["content"] if c["type"] == "image_url"] if seen else []
    check("a local server gets the same images, read again from the files, as image_url data",
          len(seen) == 1 and seen[0]["content"][0] == {"type": "text", "text": "What is this?"}
          and len(urls) == 2 and urls[0].startswith("data:image/%s;base64," % ("jpeg" if jpeg else "png"))
          and urls[1].startswith("data:image/png;base64,")
          and sent_image(urls[0].split(",", 1)[1]) == (1568, 500), [u[:40] for u in urls])
    os.remove(part)
    srv.add(answer("One image is gone."))
    run, rec = run_once(project12, "Go on.", cfg12)
    seen = [m for m in srv.requests[-1]["body"]["messages"] if isinstance(m.get("content"), list)]
    check("an image whose file was deleted becomes a line of text; the other one is still sent",
          not rec.result()["is_error"] and len(seen) == 1
          and "An image was attached here: refs/part.png" in seen[0]["content"][0]["text"]
          and len([c for c in seen[0]["content"] if c["type"] == "image_url"]) == 1, str(seen)[:300])

    notes = os.path.join(HOME, "notes.png")
    with open(notes, "w", encoding="utf-8") as f:
        f.write("this is text, not an image\n")
    before, n = chat(project12), (len(cl.requests), len(srv.requests))
    run13 = local_engine.LocalRun()
    said = ""
    try:
        run13.start(project12, "Look.", acfg, images=[notes])
    except local_engine.ImageError as exc:
        said = str(exc)
    check("a file that is no image: a plain error before anything starts",
          "`notes.png` is not an image" in said and not run13.running() and chat(project12) == before
          and (len(cl.requests), len(srv.requests)) == n, said)
    five = []
    for i in range(5):
        five.append(os.path.join(project12, "refs", "view%d.png" % i))
        picture(five[-1], 20, 20, "PNG")
    try:
        said = ""
        run13.start(project12, "Look.", acfg, images=five)
    except local_engine.ImageError as exc:
        said = str(exc)
    check("more than 4 images with one message: a plain error", said == "At most 4 images can go with one message."
          and not run13.running(), said)
    huge = os.path.join(HOME, "huge.png")
    with open(huge, "wb") as f:              # 200 bytes that declare 50000 x 50000 pixels: 10 GB when opened
        for kind, data in ((b"IHDR", struct.pack(">IIBBBBB", 50000, 50000, 8, 6, 0, 0, 0)),
                           (b"IDAT", zlib.compress(bytes(200001) * 3)), (b"IEND", b"")):
            f.write((b"" if f.tell() else b"\x89PNG\r\n\x1a\n") + struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data)))
    try:
        said = ""
        run13.start(project12, "Look.", acfg, images=[huge])
    except local_engine.ImageError as exc:
        said = str(exc)
    check("an image with too many pixels: a plain error with its size and the limit, before anything starts",
          "`huge.png` is too large: 50000 x 50000 pixels" in said and "67 million" in said
          and not run13.running() and chat(project12) == before
          and (len(cl.requests), len(srv.requests)) == n, said)
    if run13.running():
        run13.stop()
    swapped = os.path.join(project12, "refs", "view0.png")       # a good image when the message was written
    shutil.copyfile(huge, swapped)
    try:
        said = ""
        local_engine.encode_image(swapped)
    except local_engine.ImageError as exc:
        said = str(exc)
    check("a stored image whose file became too large later is refused before it is opened",
          "view0.png is too large: 50000 x 50000 pixels" in said, said)
    if jpeg:
        phone = os.path.join(HOME, "phone.jpg")
        grey = QtGui.QImage(12000, 9000, QtGui.QImage.Format_Grayscale8)
        grey.fill(QtGui.QColor(200, 200, 200))
        made = grey.save(phone, "JPEG")
        del grey
        cl.add(claude("A grey photo."))
        run, rec = run_once(project12, "And this photo?", acfg, images=[phone], timeout=60)
        sent = [b for b in cl.bodies()[-1]["messages"][-1]["content"] if b["type"] == "image"]
        check("a 108-megapixel phone photo is sent, small (Qt opens a JPEG at a part of its size)",
              made and rec.done() and not rec.result()["is_error"] and len(sent) == 1
              and sent_image(sent[0]["source"]["data"]) == (1568, 1176), rec.events)
        before = chat(project12)

    # the text that an image file holds about its author is not sent
    private = ("Jane Student", "Rue du Test 5", "CameraOwner")
    for name, kind, size in (("meta.png", "PNG", (60, 40)), ("meta-large.png", "PNG", (2000, 1500))) \
            + ((("meta.jpg", "JPEG", (60, 40)),) if jpeg else ()):
        src = os.path.join(HOME, name)
        img = QtGui.QImage(size[0], size[1], QtGui.QImage.Format_RGB32)
        img.fill(QtGui.QColor(200, 200, 200))
        img.setText("Author", "Jane Student")
        img.setText("Description", "Jane Student, Rue du Test 5")
        img.setText("CameraOwner", "Jane Student")
        on_disk = b""
        if img.save(src, kind):
            with open(src, "rb") as f:
                on_disk = f.read()
        media, data = local_engine.encode_image(src)
        raw = base64.b64decode(data)
        out = QtGui.QImage.fromData(raw)
        check("%s: the file has text about its author; what is sent has the picture only" % name,
              b"Jane Student" in on_disk and not any(word.encode() in raw for word in private)
              and not out.textKeys() and not out.isNull()
              and media == ("image/jpeg" if kind == "JPEG" else "image/png")
              and (out.width(), out.height()) == ((1568, 1176) if size[0] > 1568 else size),
              (media, out.textKeys(), [w for w in private if w.encode() in raw], b"Jane Student" in on_disk))
    for fmt, label in ((QtGui.QImage.Format_Indexed8, "a palette"), (QtGui.QImage.Format_ARGB32, "transparency"),
                       (QtGui.QImage.Format_Grayscale8, "grey"), (QtGui.QImage.Format_Mono, "black and white")):
        src = os.path.join(HOME, "kind.png")
        img = QtGui.QImage(31, 17, fmt)
        if fmt in (QtGui.QImage.Format_Indexed8, QtGui.QImage.Format_Mono):
            img.setColorTable([QtGui.QColor(250, 10, 10).rgb(), QtGui.QColor(10, 10, 250).rgb()])
            img.fill(0)
            for x in range(0, 31, 3):
                img.setPixel(x, x % 17, 1)
        else:
            img.fill(QtGui.QColor(250, 120, 10, 255))
            alpha = 90 if fmt == QtGui.QImage.Format_ARGB32 else 255
            for x in range(0, 31, 3):
                img.setPixelColor(x, x % 17, QtGui.QColor(10, 20, 250, alpha))
        img.setText("Author", "Jane Student")
        ok = img.save(src, "PNG")
        again = QtGui.QImage(src)
        media, data = local_engine.encode_image(src)
        out = QtGui.QImage.fromData(base64.b64decode(data))
        check("a PNG with %s arrives with the same pixels, without its text" % label,
              ok and media == "image/png" and same_pixels(out, again) and not out.textKeys()
              and b"Jane" not in base64.b64decode(data), (media, out.format(), again.format(), out.textKeys()))

    # stored image paths are text from a file that another person can have written
    said, seen = opened(lambda: local_engine._project_file(project12, "refs/view1.png"))
    check("(setup) a good stored path gives its file, and the file system is asked for it",
          not said and len(seen) == 2, (said, seen))
    bad = ["../../outside.png", "refs/../../outside.png", "/outside.png", "refs//view1.png", "./refs/view1.png", ""]
    if os.sep == "\\":                         # Windows: realpath would open these, a login at that host
        bad += ["\\\\127.0.0.1\\nothing\\a.png", "\\\\?\\UNC\\127.0.0.1\\nothing\\a.png", "D:\\x.png",
                "D:x.png", "refs\\..\\..\\outside.png", "refs/\\\\127.0.0.1\\nothing\\a.png"]
    got = [(text,) + opened(lambda: local_engine._project_file(project12, text)) for text in bad]
    check("a stored path that is not a plain path in the project is refused as text: no file system call",
          all("is not in the project" in said and not seen for _, said, seen in got),
          [g for g in got if "is not in the project" not in g[1] or g[2]])
    seen, real_os = [], local_engine.os
    local_engine.os = windows_paths.windows_os(seen)
    try:
        got = []
        for text in windows_paths.FOREIGN + ("refs/" + windows_paths.FOREIGN[0], "refs\\..\\..\\x.png", "\\x.png"):
            try:
                got.append((text, local_engine._project_file(windows_paths.PROJECT, text)))
            except local_engine.ImageError:
                pass
        fine = local_engine._project_file(windows_paths.PROJECT, "refs/photo 1.png")
    finally:
        local_engine.os = real_os
    check("by the path rules of Windows: a name on another computer or drive is refused and never opened",
          not got and windows_paths.outside(seen) == [] and fine == windows_paths.PROJECT + "\\refs\\photo 1.png",
          (got, seen, fine))

    outside = os.path.join(HOME, "outside.png")
    picture(outside, 20, 20, "PNG")
    stolen = dict(before[0], images=["../../outside.png"])
    with open(os.path.join(project12, ".cadprompt", "local-chat.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "messages": [stolen] + before[1:]}, f)
    cl.add(claude("ok"))
    run, rec = run_once(project12, "Go on.", acfg)
    check("a stored path that leaves the project is never read or sent",
          not rec.result()["is_error"] and '"type": "image"' not in json.dumps(cl.bodies()[-1]["messages"][0])
          and "The file is not there any more" in json.dumps(cl.bodies()[-1]["messages"][0]),
          str(cl.bodies()[-1]["messages"][0])[:300])

    # ---- 14: the gate of all outgoing requests ---------------------------------------
    say("--- 14 backends.may_contact() is asked in the main thread before every request")
    gate, real_gate = [], backends.may_contact
    backends.may_contact = lambda kind, url: gate.append(
        (kind, url, threading.get_ident() == main_thread, cl.connections)) or True
    cl.add(claude("", [("get_document_state", {}, "toolu_9")]), claude("Checked."))
    c0 = cl.connections
    run, rec = run_once(project12, "Check it.", acfg)
    check("asked before each of the two requests, in the main thread, before the connection is opened",
          rec.result()["result"] == "Checked." and gate == [("anthropic", cl.url, True, c0),
                                                             ("anthropic", cl.url, True, c0 + 1)], gate)
    backends.may_contact = lambda kind, url: gate.append((kind, url)) and False
    before, c0, c1 = chat(project12), cl.connections, srv.connections
    results = []
    for config in (acfg, cfg12):
        run, rec = run_once(project12, "Once more.", config)
        results.append(rec.result() if rec.done() else {})
    pump(timeout=0.3)
    check("may_contact() says no: a plain error for both services, no connection is opened, nothing is stored",
          all(r.get("is_error") and r.get("result", "").startswith("Nothing was sent") for r in results)
          and "Anthropic" in results[0]["result"] and (cl.connections, srv.connections) == (c0, c1)
          and gate[-2:] == [("anthropic", cl.url), ("openai", srv.url)] and chat(project12) == before,
          (results, cl.connections - c0, srv.connections - c1, gate[-2:]))
    backends.may_contact = real_gate

    # ---- 15: an answer that was cut off ------------------------------------------------
    say("--- 15 an answer that the service cut off: the half-built model is not saved")
    close_all()
    local_tools.reset()
    project15 = new_project("cut")
    cl.add(claude("", [("run_python", {"code": BOX}, "toolu_a")]), claude("A plate."))
    run, rec = run_once(project15, "Make a plate.", acfg)
    doc = App.ActiveDocument
    file15 = doc.FileName if doc is not None else ""
    check("(setup) the plate is made and saved", bool(file15) and os.path.isfile(file15) and "tool:save" in rec.kinds(),
          rec.events)
    mtime15 = os.path.getmtime(file15) if file15 else 0
    cl.add(claude("", [("run_python", {"code": 'doc.addObject("Part::Box", "Extra1")'}, "toolu_b")]),
           claude("Now the holes.", [("run_python", {"code": "for i in ra"}, "toolu_c")], stop_reason="max_tokens"))
    run, rec = run_once(project15, "Add four holes.", acfg)
    res = rec.result() if rec.done() else {}
    check("anthropic: an answer cut off in a tool call ends the request as incomplete; the half call is not run",
          res.get("is_error") is False and res.get("incomplete") is True and "Now the holes." in res["result"]
          and "cut off" in res["result"] and rec.kinds().count("tool:run_python") == 1, rec.events)
    check("... the model file is not saved, the user is told, the work is still in FreeCAD",
          bool(file15) and os.path.getmtime(file15) == mtime15 and "tool:save" not in rec.kinds()
          and ("text", local_engine.NOT_SAVED) in rec.events and doc.getObject("Extra1") is not None, rec.events)
    check("... nothing of the cut answer is stored but its text", "_cut_off" not in json.dumps(chat(project15))
          and "for i in ra" not in json.dumps(chat(project15)), str(chat(project15))[-300:])
    # ---- 16: the user's yes before a step that changes things --------------------
    say("--- 16 approval.request() is asked in the main thread before run_python and make_drawing")
    from cadprompt import approval
    close_all()
    project16 = new_project("approval")
    asked, verdict, real_request = [], [False], approval.request

    def request(tool_name, args, source):
        asked.append((tool_name, dict(args), source, threading.get_ident() == main_thread))
        return verdict[0]
    approval.request = request
    try:
        code16 = 'doc.addObject("Part::Box", "NotWanted")'
        srv.add(call("run_python", {"code": code16}, "a1"), call("get_document_state", {}, "a2"),
                answer("You did not approve the step, so I stopped."))
        n = len(srv.requests)
        run, rec = run_once(project16, "Make a box.", cfg)
        check("the engine asks approval.request(tool_name, args, source) in the main thread, with the source 'local'",
              asked == [("run_python", {"code": code16}, "local", True)], asked)
        sent = srv.requests[n + 1]["body"]["messages"][-1] if len(srv.requests) > n + 1 else {}
        check("the answer False: the model gets 'The user did not approve this step.' as the tool result",
              sent.get("role") == "tool" and sent.get("content") == approval.DECLINED == "The user did not approve this step.",
              sent)
        check("... nothing ran: no object, no document, no backup, no save, no error",
              App.ActiveDocument is None and rec.kinds() == ["tool:run_python", "tool:get_document_state", "text", "finished"]
              and rec.result().get("is_error") is False and not copies_of(project16)
              and not os.path.isdir(os.path.join(project16, "model", ".backup")), rec.events)
        check("a tool that only looks is not asked for", len(asked) == 1, asked)
        cl.add(claude("", [("make_drawing", {"sheet": "A4"}, "toolu_d")]), claude("Not approved, so no drawing."))
        run, rec = run_once(project16, "Make a drawing.", acfg)
        check("the API key source asks the same function, with the source 'apikey', also for make_drawing",
              asked[-1] == ("make_drawing", {"sheet": "A4"}, "apikey", True) and len(asked) == 2
              and rec.result().get("is_error") is False and "tool:save" not in rec.kinds(), (asked, rec.events))
        verdict[0] = True
        srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Wanted")'}, "a3"), answer("Done."))
        run, rec = run_once(project16, "Make a box now.", cfg)
        check("the answer True: the step runs and the model is saved",
              len(asked) == 3 and App.ActiveDocument is not None and App.ActiveDocument.getObject("Wanted") is not None
              and "tool:save" in rec.kinds(), rec.events)

        # Stop while the question is open, and then a yes: the step belongs to a run that is over
        run16 = local_engine.LocalRun()
        rec16 = Recorder(run16)

        def stopping(tool_name, args, source):
            run16.stop()                         # what the Stop button of the panel does
            return True
        approval.request = stopping
        srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "AfterStop")'}, "a4"))
        n = len(srv.requests)
        run16.start(project16, "One more box.", cfg)
        check("Stop while the question is open: no deadlock", pump(rec16.done, 5), rec16.events)
        pump(lambda: all(w.isFinished() for w in local_engine._threads), 2.0)
        pump(timeout=0.3)
        check("... a yes after Stop runs nothing: no object, finished once with stopped=True, no further request",
              App.ActiveDocument.getObject("AfterStop") is None
              and no_backup(rec16.kinds()) == ["tool:run_python", "finished"]
              and rec16.result().get("stopped") is True and len(srv.requests) == n + 1
              and "AfterStop" not in json.dumps(chat(project16)[-1:]), (rec16.events, len(srv.requests) - n))
    finally:
        approval.request = real_request
    # the real function, with no panel that could ask: only the user's switch says yes
    user_yes.run_without_asking(False)
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "NobodyAsked")'}, "a5"), answer("Not approved."))
    n = len(srv.requests)
    run, rec = run_once(project16, "One more.", cfg)
    sent = srv.requests[n + 1]["body"]["messages"][-1] if len(srv.requests) > n + 1 else {}
    check("the real approval.request, 'run without asking' off and no panel to ask: the step does not run",
          approval.request("run_python", {"code": "x"}, "local") is False
          and approval.request("make_drawing", {}, "account") is False
          and sent.get("content") == approval.DECLINED and App.ActiveDocument.getObject("NobodyAsked") is None
          and "tool:save" not in rec.kinds(), (sent, rec.events))
    user_yes.run_without_asking()
    check("... and with the user's switch on it runs", approval.request("run_python", {"code": "x"}, "local") is True)

    check("the Anthropic script was used exactly", cl.overrun == 0 and not cl.script, (cl.overrun, len(cl.script)))
    cl.close()

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
