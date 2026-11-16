"""The panel together with the real built-in engine (cadprompt/local_engine.py).

test_panel.py tests the panel alone, with a stub engine. This one checks that
the two fit: a message typed in the panel reaches a model server, the tool
call changes the FreeCAD document, the answer shows up, the model is saved.

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_panel_engine.py

A scripted fake server (local_fake_server.py) plays the model, so the result
does not depend on any AI. For one smoke run with a real model add
PANEL_REAL_MODEL=google/gemma-4-e4b (and PANEL_REAL_URL if it is not LM Studio
on this computer); a wrong part from a small model is reported, not failed.
"""

import faulthandler
import json
import os
import sys
import time
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

OUT = sys.__stdout__
FAILED = []
COUNT = [0]
SLOT_ERRORS = []


def say(text):
    OUT.write(text + "\n")
    OUT.flush()


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    say(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def main():
    import FreeCAD as App
    from PySide import QtCore, QtWidgets

    # see test_panel.py: without this FreeCAD 1.1 locks up when it has no display
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

    from cadprompt import backends, bridge, local_engine, panel, paths, projects, runner
    from local_fake_server import FakeServer, answer, call, slow

    home = os.environ["FREECAD_USER_HOME"]
    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(home, "projects"))
    p.SetString("Backend", "local")
    project = projects.create("Engine part")

    # the Claude Code side must stay untouched: make every way into it loud
    touched = []
    bridge.ensure = lambda: touched.append("bridge") or ("t", "bridge running")
    runner.ClaudeRun.start = lambda self, *a, **k: touched.append("claude")
    runner.auth_status = lambda *a, **k: touched.append("auth")
    paths.find_tool = lambda name: None

    box = ("d = App.ActiveDocument or App.newDocument('Model')\n"
           "b = d.addObject('Part::Box', 'Plate')\n"
           "b.Length, b.Width, b.Height = 80, 40, 6\n"
           "d.recompute()\n"
           "print('volume', b.Shape.Volume)")
    srv = FakeServer([call("run_python", {"code": box}, reasoning="thinking…"),
                      answer("A plate 80 x 40 x 6 mm, volume 19200 mm³.")])
    p.SetString("LocalUrl", srv.url)
    p.SetString("LocalModel", "fake-model")

    pan = panel.Panel()
    spin(150)
    check("panel with the real engine: ready, no setup message",
          not [e for e in pan.entries if e[0].startswith("setup")] and pan.project == project, str(pan.entries))
    pan.input.setPlainText("make a plate 80 x 40 x 6")
    pan.send()
    check("the message is on its way: 'The local model is working…', Stop",
          pan.status.text() == "The local model is working…" and pan.send_btn.text() == "Stop")
    ticks = [0]
    ticker = QtCore.QTimer()
    ticker.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    ticker.start(20)
    done = wait_for(lambda: pan.send_btn.text() == "Send", 30)
    ticker.stop()
    check("the run ends and FreeCAD kept running meanwhile", done and ticks[0] > 0, f"{ticks[0]} ticks")
    doc = App.ActiveDocument
    plate = doc.getObject("Plate") if doc else None
    check("the tool call of the model built the part in FreeCAD",
          plate is not None and abs(plate.Shape.Volume - 19200) < 1e-6)
    kinds = [k for k, _ in pan.entries]
    texts = [t for _, t in pan.entries]
    check("the panel shows: you, the tool, the answer",
          ("user", "make a plate 80 x 40 x 6") in pan.entries
          and any(k == "tool" and t.startswith("`run_python`") for k, t in pan.entries)
          and ("claude", "A plate 80 x 40 x 6 mm, volume 19200 mm³.") in pan.entries
          and "error" not in kinds, str(pan.entries))
    check("the model was saved in the project's model/ folder",
          bool(doc and doc.FileName) and os.path.dirname(os.path.realpath(doc.FileName)) ==
          os.path.realpath(os.path.join(project, "model")) and os.path.isfile(doc.FileName),
          str(doc and doc.FileName))
    sent = srv.requests[0]
    check("the server got the model name and no Authorization header (no key set)",
          sent["body"].get("model") == "fake-model" and "Authorization" not in sent["headers"]
          and sent["path"].endswith("/chat/completions"), str(sent["headers"]))
    check("the engine keeps its own conversation file; the Claude Code session file is not made",
          os.path.isfile(os.path.join(project, ".cadprompt", "local-chat.json"))
          and not os.path.exists(os.path.join(project, ".cadprompt", "session")))
    check("Claude Code, uv and the bridge were not touched", not touched, str(touched))

    # a second panel (FreeCAD restarted): the conversation comes back
    pan2 = panel.Panel()
    spin(150)
    check("a new panel shows the earlier conversation again",
          ("user", "make a plate 80 x 40 x 6") in pan2.entries
          and ("claude", "A plate 80 x 40 x 6 mm, volume 19200 mm³.") in pan2.entries
          and pan2.entries[-1] == ("note", "Continuing this conversation."), str(pan2.entries))
    pan2.deleteLater()

    # the next message continues the conversation; slash commands and the mode
    srv.add(answer("Schritt 1: der Sketch."))
    pan.send("/teach a bracket")
    wait_for(lambda: pan.send_btn.text() == "Send", 30)
    body = srv.requests[-1]["body"]
    said = json.dumps(body["messages"])
    check("the second request carries the first exchange", "make a plate 80 x 40 x 6" in said
          and "19200" in said and "reasoning_content" not in said)
    check("/teach switches the mode file and the Teach button", projects.get_mode(project) == "teach"
          and pan.teach_btn.isChecked())
    pan.build_btn.click()

    # Stop in the middle of a slow answer
    srv.add(slow(8, answer("too late")))
    pan.send("something slow")
    spin(300)
    check("(setup) the slow run is running", pan.send_btn.text() == "Stop")
    t0 = time.time()
    pan.send_btn.click()
    spin(100)
    check("Stop: at once, quiet, button back to Send",
          time.time() - t0 < 1 and pan.send_btn.text() == "Send" and pan.entries[-1] == ("note", "Stopped.")
          and "error" not in [k for k, _ in pan.entries], str(pan.entries[-2:]))

    # an unreachable server: an error in plain words, without "Claude"
    p.SetString("LocalUrl", "http://127.0.0.1:1/v1")
    pan.send("hello")
    wait_for(lambda: pan.send_btn.text() == "Send", 30)
    last = pan.entries[-1]
    check("dead server: one plain error, no 'Claude' in it: " + last[1][:70].replace("\n", " "),
          last[0] == "error" and "Claude" not in last[1] and "Traceback" not in last[1], str(last))
    p.SetString("LocalUrl", srv.url)

    # New chat forgets the conversation
    [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0].click()
    check("New chat: the engine's history is empty, the view starts over",
          local_engine.history(project) == [] and pan.entries == [("note", "New conversation.")],
          str(local_engine.history(project)))
    srv.add(answer("Fresh start."))
    pan.send("hello again")
    wait_for(lambda: pan.send_btn.text() == "Send", 30)
    check("after New chat the request no longer carries the old conversation",
          "make a plate 80 x 40 x 6" not in json.dumps(srv.requests[-1]["body"]["messages"])
          and ("claude", "Fresh start.") in pan.entries)
    check("Claude Code was not touched in any of this", not touched, str(touched))
    srv.close()

    # ---- optional: one smoke run with a real model ----
    model = os.environ.get("PANEL_REAL_MODEL", "")
    if model:
        say(f"\n-- smoke run with the real model {model} --")
        p.SetString("LocalUrl", os.environ.get("PANEL_REAL_URL", backends.LOCAL_URL))
        p.SetString("LocalModel", model)
        for d in list(App.listDocuments().values()):
            App.closeDocument(d.Name)
        smoke = projects.create("Smoke part")
        pan._select(os.path.basename(smoke), open_model=False)
        t0 = time.time()
        pan.send("Make a plate 100 mm long, 60 mm wide and 8 mm thick.")
        done = wait_for(lambda: pan.send_btn.text() == "Send", 240)
        for k, t in pan.entries:
            say(f"   {k:6} {t[:300]!r}")
        check(f"real model: the run ends by itself ({time.time() - t0:.0f} s)", done)
        check("real model: the panel shows what was asked and at least one tool call",
              ("user", "Make a plate 100 mm long, 60 mm wide and 8 mm thick.") in pan.entries
              and "tool" in [k for k, _ in pan.entries], str(pan.entries))
        # what the model built is its own doing: reported here, never a failed check
        doc = App.ActiveDocument
        say(f"   document: {doc and doc.Name}, saved as: {doc and doc.FileName}")
        right = False
        for o in (doc.Objects if doc else []):
            shape = getattr(o, "Shape", None)
            if shape is not None and not shape.isNull():
                bb = shape.BoundBox
                size = sorted(round(x, 3) for x in (bb.XLength, bb.YLength, bb.ZLength))
                right = right or size == [8.0, 60.0, 100.0]
                say(f"   object {o.Name} ({o.TypeId}): {size} mm, volume {shape.Volume:.0f} mm³")
        say("   the model " + ("built the plate that was asked for: 100 x 60 x 8" if right else
                               "did NOT build the plate that was asked for"))
        if not done:
            pan.send_btn.click()

    check("no exception in any Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))


def _hook(kind, value, tb):
    SLOT_ERRORS.append("".join(traceback.format_exception(kind, value, tb)))


if not os.environ.get("FREECAD_USER_HOME"):
    say("Set FREECAD_USER_HOME to an empty folder first: this test must not touch your FreeCAD settings.")
    os._exit(2)
sys.excepthook = _hook
faulthandler.dump_traceback_later(285, exit=True, file=OUT)
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
