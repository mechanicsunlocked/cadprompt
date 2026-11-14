"""The whole addon together, as a user meets it. Nothing is replaced by a stub.

FreeCAD loads the addon (package.xml, InitGui.py), the CADprompt command opens
the dock, and the real panel, the real built-in engine, the real tools and the
real drawing function work on a real document. Only the model is played by a
scripted server (local_fake_server.py), so the result does not depend on any AI.

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_integration_gui.py

For one more run with a real model add INTEGRATION_REAL_MODEL=google/gemma-4-e4b
(and INTEGRATION_REAL_URL if it is not LM Studio on this computer). What a
small model builds is reported, not failed; a run that hangs, a frozen window
or a lost Stop button is a failed check.

The report goes to stdout and to <FREECAD_USER_HOME>/test_integration_gui.log.
FREECAD_USER_HOME keeps the test away from the user's real FreeCAD settings.
"""

import faulthandler
import json
import math
import os
import sys
import time
import traceback

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
REPO = os.path.dirname(ADDON)
sys.path[:0] = [ADDON, HERE, os.path.join(REPO, "freecad-hoischen")]

OUT = sys.__stdout__             # FreeCAD's own sys.stdout goes to its report view
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_integration_gui.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []                 # exceptions inside Qt slots do not reach us any other way


def say(text):
    try:
        OUT.write(text + "\n")
        OUT.flush()
    except (OSError, ValueError, AttributeError):
        pass
    if os.environ.get("FREECAD_USER_HOME"):
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    say(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def load_addon(App):
    """Do what FreeCAD's start-up does with an addon folder: package.xml, then InitGui.py."""
    meta = App.Metadata(os.path.join(ADDON, "package.xml"))
    benches = meta.Content.get("workbench", [])
    check("package.xml: read by FreeCAD, fits this FreeCAD, declares one workbench entry",
          meta.Name == "CADprompt" and meta.supportsCurrentFreeCAD() and len(benches) == 1
          and benches[0].supportsCurrentFreeCAD(), f"{meta.Name} {len(benches)}")
    folder = os.path.join(ADDON, (benches[0].Subdirectory or benches[0].Name) if benches else "")
    path = os.path.join(folder, "InitGui.py")
    check("package.xml points at the folder that holds InitGui.py", os.path.isfile(path), path)
    with open(path, encoding="utf-8") as f:
        source = f.read()
    # FreeCAD runs the file inside a function: its module-level names are locals
    # there, and gone when the command's methods are called later
    exec(compile(source, path, "exec"), {"__name__": "FreeCADGuiInit"}, {})


def main():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets

    # Without a display Qt warns "This plugin does not support raise()" when FreeCAD
    # shows a notification, and FreeCAD 1.1 locks up while it reports that warning.
    QtCore.QLoggingCategory.setFilterRules("default.warning=false")

    def spin(ms=50):
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_for(cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            spin(20)
        return bool(cond())

    class Pulse:
        """A timer that shows whether FreeCAD's window stayed alive during a run."""

        def __init__(self):
            self.ticks, self.gap, self._last = 0, 0.0, time.monotonic()
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._tick)
            self.timer.start(25)

        def _tick(self):
            now = time.monotonic()
            self.ticks, self.gap, self._last = self.ticks + 1, max(self.gap, now - self._last), now

        def stop(self):
            self._tick()
            self.timer.stop()
            return self

    from cadprompt import backends, local_engine, local_tools, paths, projects
    from local_fake_server import FakeServer, answer, call, slow

    home = os.environ["FREECAD_USER_HOME"]
    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(home, "projects"))
    p.SetString("Backend", "local")
    project = projects.create("Integration plate")
    check("a project is made from the repository's template",
          os.path.realpath(paths.template_dir()) == os.path.realpath(os.path.join(REPO, "template"))
          and all(os.path.exists(os.path.join(project, n)) for n in
                  ("CLAUDE.md", "model", "refs", "drawings", os.path.join(".claude", "commands", "drawing.md"))),
          project)

    srv = FakeServer()
    p.SetString("LocalUrl", srv.url)
    p.SetString("LocalModel", "fake-model")

    # ---- 1. FreeCAD loads the addon; the command opens the dock ----
    say("\n-- the addon loads and the command opens the dock --")
    load_addon(App)
    check("InitGui.py registered the command CADprompt_Panel", "CADprompt_Panel" in Gui.listCommands())
    mw = Gui.getMainWindow()
    check("(setup) no dock before the command", mw.findChild(QtWidgets.QDockWidget, "CADpromptDock") is None)
    Gui.runCommand("CADprompt_Panel")
    spin(200)
    dock = mw.findChild(QtWidgets.QDockWidget, "CADpromptDock")
    from cadprompt import panel
    pan = dock.widget() if dock is not None else None
    check("the command made the dock with the panel in it",
          dock is not None and dock.isVisible() and isinstance(pan, panel.Panel))
    Gui.runCommand("CADprompt_Panel")
    hidden = dock is not None and not dock.isVisible()
    Gui.runCommand("CADprompt_Panel")
    check("the command hides and shows the same dock again",
          hidden and dock.isVisible() and len(mw.findChildren(QtWidgets.QDockWidget, "CADpromptDock")) == 1)
    if pan is None:
        return
    check("the panel is ready for the local model: project chosen, no setup message, source shown",
          pan.project == project and not [e for e in pan.entries if e[0].startswith("setup")]
          and pan.source.full == "Local model · fake-model", f"{pan.entries} {pan.source.full!r}")

    def idle():
        return not pan._running() and pan.send_btn.text() == "Send" and pan.status.text() == ""

    def user_texts(body):
        return [m["content"] for m in body["messages"] if m["role"] == "user"]

    # ---- 2. a plate and its drawing ----
    say("\n-- local model (scripted): a plate and a drawing --")
    code = ('plate = doc.addObject("Part::Box", "Plate")\n'
            'plate.Label = "Plate"\n'
            'plate.Length, plate.Width, plate.Height = 100, 50, 10')
    srv.add(call("run_python", {"code": code}, call_id="c1", reasoning="A box will do."),
            call("make_drawing", {"sheet": "A4"}, call_id="c2"),
            answer("The plate 100 x 50 x 10 mm is built and its drawing is in drawings/."))
    check("(setup) no document is open and the panel is idle", App.ActiveDocument is None and idle())
    pulse = Pulse()
    t0 = time.time()
    pan.input.setPlainText("make a plate and a drawing")
    pan.send()
    check("after Send: 'The local model is working…' and the button says Stop",
          pan.status.text() == "The local model is working…" and pan.send_btn.text() == "Stop")
    done = wait_for(idle, 120)
    pulse.stop()
    say(f"      {time.time() - t0:.1f} s, {pulse.ticks} timer ticks, longest pause {pulse.gap:.2f} s")
    check("the run ends and the status goes back to idle (empty status, Send)", done,
          f"{pan.status.text()!r} {pan.send_btn.text()!r}")
    check("FreeCAD's window stayed alive during the run", pulse.ticks > 5 and pulse.gap < 5,
          f"{pulse.ticks} ticks, {pulse.gap:.2f} s")
    doc = App.ActiveDocument
    plate = doc.getObject("Plate") if doc else None
    bb = plate.Shape.BoundBox if plate is not None else None
    check("the box exists: Part::Box 'Plate', 100 x 50 x 10 mm",
          plate is not None and plate.TypeId == "Part::Box" and plate.Label == "Plate"
          and (round(bb.XLength, 6), round(bb.YLength, 6), round(bb.ZLength, 6)) == (100, 50, 10)
          and abs(plate.Shape.Volume - 50000) < 1e-6)
    model_dir = os.path.realpath(os.path.join(project, "model"))
    models = sorted(n for n in os.listdir(model_dir) if n.endswith(".FCStd")) if os.path.isdir(model_dir) else []
    check("the model file is in the project's model/ folder and is the open document",
          len(models) == 1 and bool(doc and doc.FileName)
          and os.path.realpath(doc.FileName) == os.path.join(model_dir, models[0]), f"{models} {doc and doc.FileName}")
    pdf = os.path.join(project, "drawings", "Plate-A4.pdf")
    head = b""
    if os.path.isfile(pdf):
        with open(pdf, "rb") as f:
            head = f.read(4)
    check("the PDF is in the project's drawings/ folder", head == b"%PDF" and os.path.getsize(pdf) > 5000, pdf)
    pages = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")] if doc else []
    check("the document holds the drawing page 'Drawing Plate' with dimensions",
          [o.Label for o in pages] == ["Drawing Plate"]
          and any(o.isDerivedFrom("TechDraw::DrawViewDimension") for o in doc.Objects))
    check("the saved model file was written after the drawing was made (the page is in it)",
          bool(models) and os.path.isfile(pdf)
          and os.path.getmtime(os.path.join(model_dir, models[0])) >= os.path.getmtime(pdf))
    kinds = [k for k, _ in pan.entries]
    tools = [t for k, t in pan.entries if k == "tool"]
    check("the panel shows the user line, the tool lines and the answer, in that order, and no error",
          ("user", "make a plate and a drawing") in pan.entries
          and len(tools) == 3 and tools[0].startswith("`run_python`") and tools[1] == "`make_drawing` A4"
          and tools[2] == "`save` model/" + (models[0] if models else "?")
          and ("claude", "The plate 100 x 50 x 10 mm is built and its drawing is in drawings/.") in pan.entries
          and kinds.index("user") < kinds.index("tool") < kinds.index("claude")
          and "error" not in kinds, str(pan.entries))
    check("the chat view shows these lines",
          all(w in pan.view.toPlainText() for w in ("make a plate and a drawing", "run_python", "make_drawing",
                                                    "its drawing is in drawings/")))
    check("no .cadprompt/session file (that is Claude Code's); the engine's own conversation file is there",
          not os.path.exists(os.path.join(project, ".cadprompt", "session"))
          and os.path.isfile(os.path.join(project, ".cadprompt", "local-chat.json")))
    check("Claude Code was never started", pan.run.proc is None and not pan.run.running() and pan.claude is None)
    bodies = srv.bodies()
    results = [m["content"] for m in bodies[-1]["messages"] if m["role"] == "tool"] if len(bodies) == 3 else []
    check("the model got both tool results: the new object, and the drawing report with scale and PDF path",
          len(results) == 2 and "+ new Plate: Part::Box, size 100.000 x 50.000 x 10.000" in results[0]
          and results[1].startswith("Drawing made:") and "Scale: 1:1" in results[1]
          and "PDF: " + pdf in results[1] and "Open points:" in results[1] and "ERROR" not in results[1],
          str(results)[:1500])
    check("the system prompt and the four tools went to the server; the model's thinking never came back",
          bodies and bodies[0]["messages"][0]["role"] == "system"
          and "make_drawing" in bodies[0]["messages"][0]["content"]
          and sorted(t["function"]["name"] for t in bodies[0]["tools"]) ==
          ["get_document_state", "make_drawing", "read_project_file", "run_python"]
          and "reasoning_content" not in json.dumps(bodies) and "A box will do." not in json.dumps(bodies))
    undo = list(doc.UndoNames) if doc else []
    check("the work is on FreeCAD's undo list: the model's step and the drawing steps",
          "CADprompt" in undo and "Make drawing" in undo, str(undo))

    # ---- 3. the Drawing button: /drawing A3 ----
    say("\n-- the Drawing button, A3: the slash command reaches the server expanded --")
    srv.add(call("make_drawing", {"sheet": "A3"}, call_id="c3"),
            answer("The A3 drawing is ready. Tolerances and material are still open."))
    before = len(srv.requests)
    button = [b for b in pan.findChildren(QtWidgets.QPushButton) if b.text() == "Drawing"][0]
    [a for a in button.menu().actions() if a.text() == "A3"][0].trigger()
    check("(setup) the button started a run", pan.send_btn.text() == "Stop")
    done = wait_for(idle, 120)
    check("the run ends and the panel is idle again", done and len(srv.requests) == before + 2,
          f"{len(srv.requests) - before} requests")
    sent = user_texts(srv.requests[before]["body"]) if len(srv.requests) > before else [""]
    check("the server got the text of .claude/commands/drawing.md, not '/drawing A3'",
          "Make a workshop drawing of the current part" in sent[-1]
          and "Sheet size and wishes: A3" in sent[-1]
          and "$ARGUMENTS" not in sent[-1] and "argument-hint" not in sent[-1]
          and not sent[-1].lstrip().startswith(("/", "---")), sent[-1])
    check("with the note that sends a local model to its make_drawing tool", "make_drawing tool" in sent[-1], sent[-1])
    check("the request carries the conversation so far", sent[0] == "make a plate and a drawing"
          and "Drawing made:" in json.dumps(srv.requests[before]["body"]["messages"]) if sent else False)
    check("the panel and the stored history show what the user did: /drawing A3",
          pan.entries.count(("user", "/drawing A3")) == 1
          and ("tool", "`make_drawing` A3") in pan.entries
          and ("user", "/drawing A3") in local_engine.history(project)
          and not any("Make a workshop drawing" in t for _, t in pan.entries), str(pan.entries[-5:]))
    pdf3 = os.path.join(project, "drawings", "Plate-A3.pdf")
    pages = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]
    check("the A3 PDF is there, the A4 one is kept, and the document still has one drawing page",
          os.path.isfile(pdf3) and os.path.getsize(pdf3) > 5000 and os.path.isfile(pdf)
          and [o.Label for o in pages] == ["Drawing Plate"], str([o.Label for o in pages]))
    check("no session file, no error line, the mode is still build",
          not os.path.exists(os.path.join(project, ".cadprompt", "session"))
          and "error" not in [k for k, _ in pan.entries] and projects.get_mode(project) == "build"
          and pan.build_btn.isChecked())

    # ---- 4. Stop ----
    say("\n-- Stop during a slow answer --")
    srv.add(slow(8, call("run_python", {"code": 'doc.addObject("Part::Box", "TooLate")'}, call_id="c4")))
    objects = len(doc.Objects)
    pan.send("something slow")
    spin(400)
    check("(setup) the slow run is running and the button says Stop",
          pan._running() and pan.send_btn.text() == "Stop" and pan.send_btn.isEnabled())
    t0 = time.time()
    pan.send_btn.click()
    back = wait_for(idle, 2)
    took = time.time() - t0
    check(f"Stop: the panel is idle again within 2 s ({took:.2f} s), with the quiet note 'Stopped.'",
          back and took < 2 and pan.entries[-1] == ("note", "Stopped.")
          and "error" not in [k for k, _ in pan.entries], str(pan.entries[-2:]))
    srv.add(answer("Still here."))
    pan.send("are you there?")
    done = wait_for(idle, 30)
    check("a new message works after Stop", done and pan.entries[-1] == ("claude", "Still here."),
          str(pan.entries[-2:]))
    spin(300)
    check("the stopped answer never arrived: no late tool run, no late object",
          doc.getObject("TooLate") is None and len(doc.Objects) == objects
          and not any("TooLate" in t for _, t in pan.entries))
    wait_for(lambda: not [w for w in local_engine._threads if not w.isFinished()], 10)
    check("no worker thread is left running", not [w for w in local_engine._threads if not w.isFinished()])
    srv.close()

    # ---- 5. optional: one run with a real model ----
    model = os.environ.get("INTEGRATION_REAL_MODEL", "")
    if model:
        real_run(App, QtCore, pan, model, Pulse, wait_for, idle, backends, local_engine, local_tools, paths,
                 projects)

    check("no exception in any Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))


def real_run(App, QtCore, pan, model, Pulse, wait_for, idle, backends, local_engine, local_tools, paths, projects):
    prompt = "Make a plate 100 x 50 x 10 mm with a 20 mm hole in the centre."
    limit = float(os.environ.get("INTEGRATION_REAL_SECONDS", "240"))
    say(f"\n-- one run with the real model {model}: {prompt!r}, limit {limit:.0f} s --")
    p = paths.params()
    p.SetString("LocalUrl", os.environ.get("INTEGRATION_REAL_URL", backends.LOCAL_URL))
    p.SetString("LocalModel", model)
    for d in list(App.listDocuments().values()):
        App.closeDocument(d.Name)
    project = projects.create("Real plate")
    pan._select(os.path.basename(project), open_model=False)
    pan._show_source()

    seen = {"stop": 0, "samples": 0}
    watch = QtCore.QTimer()

    def sample():      # is the Stop button there for the whole run?
        if pan._running():
            seen["samples"] += 1
            seen["stop"] += pan.send_btn.text() == "Stop" and pan.send_btn.isEnabled() and pan.send_btn.isVisibleTo(pan)

    watch.timeout.connect(sample)
    watch.start(200)
    shown, stamps = [0], []

    def progress():
        while shown[0] < len(pan.entries):
            kind, text = pan.entries[shown[0]]
            stamps.append((time.time() - t0, kind))
            say("   [%5.1f s] %-6s %s" % (time.time() - t0, kind, text.replace("\n", "\n" + " " * 21)[:1200]))
            shown[0] += 1
        return idle()

    pulse = Pulse()
    t0 = time.time()
    pan.send(prompt)
    done = wait_for(progress, limit)
    took = time.time() - t0
    pulse.stop()
    watch.stop()
    progress()
    kinds = [k for k, _ in pan.entries]
    say(f"   time: {took:.1f} s; tool calls: {kinds.count('tool')}; failed tool calls: "
        f"{sum(1 for k, t in pan.entries if k == 'error' and 'failed' in t)}; "
        f"timer ticks: {pulse.ticks}; longest pause of the window: {pulse.gap:.2f} s")
    check(f"real model: the run ends by itself within {limit:.0f} s ({took:.0f} s)", done)
    if not done:
        pan.send_btn.click()
        check("real model: Stop ends the run that took too long", wait_for(idle, 2))
    check("real model: the window stayed alive (no pause longer than 5 s)", pulse.ticks > 5 and pulse.gap < 5,
          f"{pulse.gap:.2f} s")
    check("real model: the Stop button was there for the whole run",
          seen["samples"] > 0 and seen["stop"] == seen["samples"], str(seen))
    check("real model: the panel shows the request and no traceback of our own code",
          ("user", prompt) in pan.entries
          and not any("The local engine failed" in t or "ERROR in tool" in t for _, t in pan.entries))
    check("real model: no Claude Code session file", not os.path.exists(os.path.join(project, ".cadprompt", "session")))
    wait_for(lambda: not [w for w in local_engine._threads if not w.isFinished()], 10)
    check("real model: no worker thread is left running",
          not [w for w in local_engine._threads if not w.isFinished()])

    # what the model built is its own doing: reported here, never a failed check
    doc = App.ActiveDocument
    say("   final document state:")
    for line in local_tools.get_document_state().splitlines():
        say("      " + line)
    say(f"   saved as: {doc.FileName if doc is not None and doc.FileName else 'not saved'}")
    want = 100 * 50 * 10 - math.pi * 10 ** 2 * 10
    right = None
    for o in (doc.Objects if doc is not None else []):
        shape = getattr(o, "Shape", None)
        if shape is None or shape.isNull() or not shape.Solids:
            continue
        bb = shape.BoundBox
        size = sorted(round(x, 3) for x in (bb.XLength, bb.YLength, bb.ZLength))
        if size == [10.0, 50.0, 100.0] and abs(shape.Volume - want) < 1 and shape.isValid():
            c = shape.CenterOfMass
            centred = abs(c.x - bb.Center.x) < 0.01 and abs(c.y - bb.Center.y) < 0.01
            right = (o.Name, centred)
    if right:
        say(f"   MODEL RESULT: right. {right[0]} is 100 x 50 x 10 mm with the volume of a 20 mm through hole "
            f"({want:.1f} mm³)" + (", hole in the centre." if right[1] else ", but the hole is NOT in the centre."))
    else:
        say(f"   MODEL RESULT: wrong or incomplete. No valid solid of 100 x 50 x 10 mm with volume {want:.1f} mm³.")


def _hook(kind, value, tb):
    SLOT_ERRORS.append("".join(traceback.format_exception(kind, value, tb)))


if not os.environ.get("FREECAD_USER_HOME"):
    say("Set FREECAD_USER_HOME to an empty folder first: this test must not touch your FreeCAD settings.")
    os._exit(2)
open(LOG, "w").close()
sys.excepthook = _hook
faulthandler.dump_traceback_later(295, exit=True, file=OUT)
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
