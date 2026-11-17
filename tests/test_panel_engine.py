# RUN: freecad-gui
"""The panel together with the real built-in engine (cadprompt/local_engine.py).

test_panel.py tests the panel alone, with a stub engine. This one checks that
the two fit: a message typed in the panel reaches a model server, the tool
call changes the FreeCAD document, the answer shows up, the model is saved.

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_panel_engine.py

A scripted fake server (local_fake_server.py) plays the model, so the result
does not depend on any AI. The second half does the same with the Anthropic
API key source and a scripted Anthropic server: no Claude Code is started.
For one smoke run with a real model add PANEL_REAL_MODEL=google/gemma-4-e4b
(and PANEL_REAL_URL if it is not LM Studio on this computer); a wrong part
from a small model is reported, not failed. The smoke run also sends one
image, a FreeCAD picture of a plate, and reports what the model says of it.
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

from utf8_output import utf8_output            # noqa: E402
import hang_report                              # noqa: E402

utf8_output()

OUT = sys.__stdout__
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_panel_engine.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []


def say(text):
    try:                         # FreeCAD's window program can be without any output (Windows)
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


def main():
    import FreeCAD as App
    from PySide import QtCore, QtGui, QtWidgets

    # see test_panel.py: without this FreeCAD 1.1 locks up when it has no display
    QtCore.QLoggingCategory.setFilterRules("default.warning=false")

    def spin(ms=50):
        # No event loop of our own: on macOS a QEventLoop that is started here never
        # came back once a document had its 3D view (the run itself had ended and the
        # model was saved). processEvents() is what test_local_engine.py uses there.
        end = time.monotonic() + ms / 1000.0
        while True:
            QtWidgets.QApplication.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.005)

    def wait_for(cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            spin(20)
        return bool(cond())

    from cadprompt import backends, bridge, local_engine, panel, paths, projects, runner
    from local_fake_server import FakeAnthropic, FakeServer, answer, call, claude, claude_error, slow

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

    # ---- the Anthropic API key: the same panel and engine, and no Claude Code ----
    say("\n-- the API key source: the built-in engine speaks to Anthropic --")
    disc = ("d = App.ActiveDocument or App.newDocument('Disc')\n"
            "c = d.addObject('Part::Cylinder', 'Disc')\n"
            "c.Radius, c.Height = 25, 5\n"
            "d.recompute()")
    cl = FakeAnthropic([claude("I make the disc.", [("run_python", {"code": disc}, "toolu_1")], thinking="one"),
                        claude("A disc Ø50 x 5 mm.")])
    idle = FakeServer()                     # the local server of the settings: it must see nothing
    p.SetString("LocalUrl", idle.url)
    backends.set_local_key("LOCAL-KEY-made-up")
    backends.set_api_key("sk-ant-made-up")
    real_url, backends.ANTHROPIC_URL = backends.ANTHROPIC_URL, cl.url
    p.SetString("Backend", "apikey")
    p.SetString("ApiModel", "claude-fake-1")
    for d in list(App.listDocuments().values()):
        App.closeDocument(d.Name)
    api_project = projects.create("Api part")
    pan._select(os.path.basename(api_project), open_model=False)
    pan._show_source()
    check("API key: ready with a key and a model; Claude Code, uv and the bridge are not looked for",
          pan.check_setup() and pan.source.full == "Anthropic API key · claude-fake-1" and not touched,
          f"{pan.entries} {pan.source.full} {touched}")
    sketch = os.path.join(home, "disc sketch.png")
    drawn = QtGui.QImage(64, 48, QtGui.QImage.Format_RGB32)
    drawn.fill(QtCore.Qt.white)
    drawn.save(sketch, "PNG")
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(sketch)])
    pan.take_mime(mime)
    pan.input.setPlainText("make this disc")
    pan.send()
    check("API key: the message is on its way: 'Claude is working…', Stop",
          pan.status.text() == "Claude is working…" and pan.send_btn.text() == "Stop", pan.status.text())
    done = wait_for(lambda: pan.send_btn.text() == "Send", 30)
    doc = App.ActiveDocument
    made = doc.getObject("Disc") if doc else None
    check("API key: the run ends, and the tool call of the model built the part in FreeCAD",
          done and made is not None and abs(made.Shape.Volume - 3.141592653589793 * 25 * 25 * 5) < 1e-3,
          str(pan.entries))
    check("API key: the panel shows: you, the image note, the tool, the answer, and no error",
          ("user", "make this disc") in pan.entries and ("note", "with 1 image") in pan.entries
          and any(k == "tool" and t.startswith("`run_python`") for k, t in pan.entries)
          and ("claude", "A disc Ø50 x 5 mm.") in pan.entries
          and "error" not in [k for k, _ in pan.entries], str(pan.entries))
    sent = cl.requests[0] if cl.requests else {"headers": {}, "body": {"messages": [{"content": []}]}, "path": ""}
    head = {k.lower(): v for k, v in sent["headers"].items()}
    check("API key: the request went to the Messages API with the key, the model and the image",
          sent["path"] == "/v1/messages" and head.get("x-api-key") == "sk-ant-made-up"
          and "authorization" not in head and sent["body"].get("model") == "claude-fake-1"
          and [b["type"] for b in sent["body"]["messages"][0]["content"]] == ["image", "text"],
          f"{sent['path']} {head} {str(sent['body'])[:200]}")
    check("API key: the key went to that one service; the local server saw nothing, its key stayed at home",
          idle.connections == 0 and "LOCAL-KEY" not in json.dumps(cl.requests)
          and len(cl.requests) == 2, f"{idle.connections} {len(cl.requests)}")
    check("API key: Claude Code, uv and the bridge were not touched; no Claude Code session file is made",
          not touched and not os.path.exists(os.path.join(api_project, ".cadprompt", "session"))
          and os.path.isfile(os.path.join(api_project, ".cadprompt", "local-chat.json")), str(touched))
    cl.add(claude_error(401, "authentication_error", "invalid x-api-key"))
    pan.send("hello")
    wait_for(lambda: pan.send_btn.text() == "Send", 30)
    last = pan.entries[-1]
    check("API key, refused key: one plain error that names Anthropic, not a 'local model': " + last[1][:60],
          last[0] == "error" and "Anthropic did not accept the API key" in last[1] and "local" not in last[1]
          and "sk-ant" not in last[1], str(last))

    # the source changes in the middle of the project: the conversation goes with it
    p.SetString("Backend", "local")
    p.SetString("LocalModel", "fake-model")
    pan._show_source()
    idle.add(answer("It is a disc."))
    pan.send("what did you make?")
    check("back on the local model: 'The local model is working…'", pan.status.text() == "The local model is working…")
    wait_for(lambda: pan.send_btn.text() == "Send", 30)
    body = idle.requests[-1]["body"]["messages"] if idle.requests else []
    there = {k.lower(): v for k, v in (idle.requests[-1]["headers"] if idle.requests else {}).items()}
    check("the local model goes on with the conversation that Anthropic began, image and tool call included",
          ("claude", "It is a disc.") in pan.entries
          and [m["role"] for m in body] == ["system", "user", "assistant", "tool", "assistant", "user"]
          and isinstance(body[1]["content"], list) and body[1]["content"][1]["type"] == "image_url"
          and body[2]["tool_calls"][0]["id"] == "toolu_1", [m["role"] for m in body])
    check("the local server got its own key and never the Anthropic key",
          there.get("authorization") == "Bearer LOCAL-KEY-made-up" and "x-api-key" not in there
          and "sk-ant" not in json.dumps(idle.requests), str(there))
    backends.ANTHROPIC_URL = real_url
    backends.set_local_key("")
    backends.set_api_key("")
    cl.close()
    idle.close()

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
            spin(500)

        # one image: a picture of a plate with four holes that FreeCAD makes here
        say(f"\n-- smoke run with the real model {model}: one image --")
        for d in list(App.listDocuments().values()):
            App.closeDocument(d.Name)
        shot = os.path.join(home, "plate with holes.png")
        pic = App.newDocument("Picture")
        plate = pic.addObject("Part::Box", "Plate")
        plate.Length, plate.Width, plate.Height = 100, 60, 8
        holes = []
        for i, (x, y) in enumerate(((15, 15), (85, 15), (15, 45), (85, 45))):
            hole = pic.addObject("Part::Cylinder", "Hole%d" % i)
            hole.Radius, hole.Height = 5, 8
            hole.Placement.Base = App.Vector(x, y, 0)
            holes.append(hole)
        tools = pic.addObject("Part::MultiFuse", "Holes")
        tools.Shapes = holes
        cut = pic.addObject("Part::Cut", "Plate_drilled")
        cut.Base, cut.Tool = plate, tools
        pic.recompute()
        # FreeCAD's own picture of the part: its hidden-line projection, seen from the side
        # and above. The 3D view cannot be used: without a display it has no OpenGL.
        how = "FreeCAD's hidden-line projection (TechDraw.projectToSVG)"
        try:
            import TechDraw
            from PySide import QtSvg
            lines = TechDraw.projectToSVG(cut.Shape, App.Vector(1, -1, 1))
            lines = lines.replace("<g ", '<g id="part" ', 1).replace('stroke-width="1.0"', 'stroke-width="0.6"')
            lines = lines.replace('transform="scale(1, -1)"', "")       # turned over below instead
            renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(
                ('<svg xmlns="http://www.w3.org/2000/svg">%s</svg>' % lines).encode("utf-8")))
            box = renderer.boundsOnElement("part").adjusted(-6, -6, 6, 6)
            renderer.setViewBox(box)
            shown = QtGui.QImage(640, int(640 * box.height() / box.width()), QtGui.QImage.Format_RGB32)
            shown.fill(QtCore.Qt.white)
            painter = QtGui.QPainter(shown)
            renderer.render(painter)
            painter.end()
            shown = shown.mirrored(False, True)
            if len({shown.pixel(x, y) for x in range(0, shown.width(), 4) for y in range(0, shown.height(), 4)}) < 2:
                raise ValueError("the picture is empty")
        except Exception as e:
            say(f"   no projection ({e}): the edges of the shape are drawn from above")
            how = "the edges of the FreeCAD shape, drawn from above"
            shown = QtGui.QImage(640, 480, QtGui.QImage.Format_RGB32)
            shown.fill(QtCore.Qt.white)
            painter = QtGui.QPainter(shown)
            painter.setPen(QtGui.QPen(QtCore.Qt.black, 3))
            for edge in cut.Shape.Edges:
                pts = [(70 + 5 * v.x, 400 - 5 * v.y) for v in edge.discretize(40)]
                for a, b in zip(pts, pts[1:]):
                    painter.drawLine(QtCore.QPointF(*a), QtCore.QPointF(*b))
            painter.end()
        shown.save(shot, "PNG")
        say(f"   image: {os.path.basename(shot)}, {shown.width()} x {shown.height()} px, "
            f"{os.path.getsize(shot)} bytes, from {how}")
        App.closeDocument(pic.Name)
        vision = projects.create("Smoke image")
        pan._select(os.path.basename(vision), open_model=False)
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(shot)])
        pan.take_mime(mime)
        ask = ("Look at the image that comes with this message. Do not use a tool. Say in two "
               "sentences what part it shows and how many holes it has.")
        t0, first = time.time(), len(pan.entries)
        pan.send(ask)
        done = wait_for(lambda: pan.send_btn.text() == "Send", 240)
        for k, t in pan.entries[first:]:
            say(f"   {k:6} {t[:600]!r}")
        check(f"real model with an image: the run ends by itself ({time.time() - t0:.0f} s)", done)
        check("real model with an image: the panel shows the note 'with 1 image'",
              ("note", "with 1 image") in pan.entries[first:], str(pan.entries[first:]))
        stored = local_engine._load(vision)
        check("real model with an image: the conversation file has the path of the image, not its data",
              bool(stored) and stored[0].get("images", [""])[0].startswith("refs/")
              and len(json.dumps(stored)) < 20000, str(stored)[:300])
        said = [t for k, t in pan.entries[first:] if k in ("claude", "error")]
        say("   the model said of the image: " + (" | ".join(said) if said else "(nothing)"))
        if not done:
            pan.send_btn.click()

    check("no exception in any Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))


def _hook(kind, value, tb):
    SLOT_ERRORS.append("".join(traceback.format_exception(kind, value, tb)))


if not os.environ.get("FREECAD_USER_HOME"):
    say("Set FREECAD_USER_HOME to an empty folder first: this test must not touch your FreeCAD settings.")
    os._exit(2)
sys.excepthook = _hook
hang_report.watch(240, os.environ["FREECAD_USER_HOME"], say)
faulthandler.dump_traceback_later(285, exit=True, file=OUT)
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
