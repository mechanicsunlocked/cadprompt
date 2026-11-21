# RUN: freecad-gui
"""The training data with the real built-in engine, the panel and the settings dialog.

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_training_gui.py

Scripted servers (local_fake_server.py) play the model and Anthropic. The
checks: off in a new profile and nothing is written; on: one line of JSON for
each request that ended well, and none for a request with a problem; what is
never in a line (a key, the token of the bridge, pixels, the text of a project
file, the paths of the project and of the home folder); a link in place of the
folder or of the file; the one line in the chat; the check box in Settings.
test_training.py tests the writer alone. Every key, name and path is made up.
"""

import base64
import faulthandler
import json
import os
import shutil
import sys
import time
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
sys.path[:0] = [ADDON, HERE]

from utf8_output import utf8_output            # noqa: E402
import hang_report                              # noqa: E402

utf8_output()

OUT = sys.__stdout__
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_training_gui.log")
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

    def pump(cond=lambda: False, seconds=10.0):
        # no event loop of our own: see test_panel_engine.py (macOS)
        end = time.time() + seconds
        while time.time() < end:
            QtWidgets.QApplication.processEvents()
            if cond():
                return True
            time.sleep(0.005)
        return bool(cond())

    from cadprompt import approval, backends, bridge, local_engine, panel, paths, projects, training
    from cadprompt.settings_dialog import SettingsDialog
    from local_fake_server import FakeAnthropic, FakeServer, answer, call, claude, cut, error, slow
    import user_yes

    home = os.environ["FREECAD_USER_HOME"]
    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(home, "projects"))
    p.SetString("Backend", "local")
    # This file is not about the question "Run or Skip?": the user of this scratch profile
    # turned "Run the AI's code without asking" on. The Skip case below answers by itself.
    user_yes.run_without_asking()

    class Recorder:
        """Writes down what a LocalRun says, in order."""

        def __init__(self, run):
            self.events = []
            run.text.connect(lambda t: self.events.append(("text", t)))
            run.tool.connect(lambda n, d: self.events.append(("tool", n, d)))
            run.finished.connect(lambda r: self.events.append(("finished", dict(r))))

        def done(self):
            return any(e[0] == "finished" for e in self.events)

        def result(self):
            return [e[1] for e in self.events if e[0] == "finished"][-1]

        def saved(self):
            return any(e[:2] == ("tool", "save") for e in self.events)

        def texts(self):
            return [e[1] for e in self.events if e[0] == "text"]

    def new_project(name):
        path = os.path.join(home, "projects", name)
        shutil.copytree(os.path.join(ADDON, "template"), path)
        return path

    def run_once(project, prompt, config, images=None):
        run = local_engine.LocalRun()
        rec = Recorder(run)
        run.start(project, prompt, config, images=images)
        pump(rec.done, 20)
        return rec

    def raw(project):
        try:
            with open(os.path.join(project, "training", "requests.jsonl"), "rb") as f:
                return f.read()
        except OSError:
            return None

    def rows(project):
        return [json.loads(row) for row in (raw(project) or b"").decode("utf-8").split("\n") if row]

    def close_all():
        for name in list(App.listDocuments()):
            App.closeDocument(name)

    def box(name):
        return call("run_python", {"code": 'b = doc.addObject("Part::Box", "%s")\nprint("made", b.Name)' % name},
                    "call_" + name)

    srv = FakeServer()
    # room for the whole conversation of this test: what a full context does is tested in test_training.py
    cfg = {"kind": "openai", "url": srv.url, "model": "fake-model", "key": "", "context_chars": 60000}
    SAVED = "*%s*" % training.SAVED

    # ---- 1: off in a new profile ---------------------------------------------------
    say("--- 1 a new profile: the training data is off, nothing is written")
    one = new_project("one")
    srv.add(box("First"), answer("A box, 10 x 10 x 10 mm."))
    rec = run_once(one, "make a box", cfg)
    check("(setup) the request ended well and the model was saved",
          rec.result() == {"is_error": False, "result": "A box, 10 x 10 x 10 mm."} and rec.saved(), rec.events)
    check("off by default: no folder training/ in the project, and no line in the chat about it",
          training.enabled() is False and not os.path.lexists(os.path.join(one, "training"))
          and not [t for t in rec.texts() if "training" in t], str(rec.texts()))

    # ---- 2: on ------------------------------------------------------------------------
    say("--- 2 on: one line of JSON for each request that ended well")
    training.set_enabled(True)
    n = len(srv.requests)
    srv.add(box("Second"), answer("A second box."))
    rec = run_once(one, "add a second box", cfg)
    data, got = raw(one), rows(one)
    check("a good request: training/requests.jsonl has one line, UTF-8, with LF at its end",
          rec.saved() and data is not None and data.endswith(b"\n") and data.count(b"\n") == 1 and b"\r" not in data
          and len(got) == 1, repr(data)[:200])
    row = got[0] if got else {"messages": []}
    body = srv.requests[n]["body"] if len(srv.requests) > n else {"messages": [{}], "tools": []}
    check("the line has: version 1, the time in UTC, the source, the model as it is set, the mode",
          row.get("version") == 1 and str(row.get("time", "")).endswith("Z") and len(row.get("time", "")) == 20
          and (row.get("source"), row.get("model"), row.get("mode")) == ("local", "fake-model", "build"), str(row)[:200])
    check("... the system prompt that the server got, and the names of the tools that it got",
          row.get("system") == body["messages"][0].get("content") and len(row.get("system") or "") > 500
          and row.get("tools") == [t["function"]["name"] for t in body["tools"]]
          == ["run_python", "get_document_state", "read_project_file", "make_drawing"], str(row.get("tools")))
    shape = [(m.get("role"), bool(m.get("context")), bool(m.get("tool_calls"))) for m in row["messages"]]
    check("... the first request as context, then this one: user, assistant with its tool call, tool result, answer",
          shape == [("user", True, False), ("assistant", True, True), ("tool", True, False), ("assistant", True, False),
                    ("user", False, False), ("assistant", False, True), ("tool", False, False), ("assistant", False, False)],
          str(shape))
    mine = row["messages"][4:] if len(row["messages"]) == 8 else [{}, {"tool_calls": [{"function": {}}]}, {}, {}]
    fn = mine[1]["tool_calls"][0]["function"]
    check("... with the user's words, the code as the model sent it, what the tool reported, and the final answer",
          mine[0] == {"role": "user", "content": "add a second box"} and fn.get("name") == "run_python"
          and json.loads(fn.get("arguments") or "{}") == json.loads(box("Second")["tool_calls"][0]["function"]["arguments"])
          and mine[2].get("tool_call_id") == mine[1]["tool_calls"][0].get("id") == "call_Second"
          and "made Second" in mine[2].get("content", "") and "+ new Second" in mine[2].get("content", "")
          and mine[3] == {"role": "assistant", "content": "A second box."}, str(mine)[:400])
    check("the chat names the file, in one line",
          rec.texts().count(SAVED) == 1 and "training/requests.jsonl" in SAVED and "Nothing is sent" in SAVED, str(rec.texts()))
    srv.add(box("Third"), answer("A third box."))
    rec = run_once(one, "add a third box", cfg)
    check("the next good request is the second line, and the chat does not say it again",
          len(rows(one)) == 2 and raw(one).startswith(data) and SAVED not in rec.texts()
          and rows(one)[1]["messages"][-1]["content"] == "A third box.", str(rec.texts()))
    check("nothing was sent for it: the server got only the requests of the three messages",
          len(srv.requests) == 6 and not srv.gets, len(srv.requests))

    # ---- 3: a request with a problem writes nothing --------------------------------------
    say("--- 3 each kind of problem: no line")
    kept = raw(one)

    def nothing_new(name, ok=True, detail=""):
        check("%s: nothing is written" % name, ok and raw(one) == kept, detail or "%d lines" % len(rows(one)))

    srv.add(box("E1"), error(500, "GPU ran out of memory"))
    rec = run_once(one, "more", cfg)
    nothing_new("an error of the engine after a step", rec.result().get("is_error") is True, rec.events)

    srv.add(box("E2"), slow(8, answer("too late")))
    run = local_engine.LocalRun()
    rec = Recorder(run)
    seen = len(srv.requests)
    run.start(one, "more", cfg)
    pump(lambda: len(srv.requests) >= seen + 2 and App.ActiveDocument.getObject("E2") is not None, 10)
    pump(seconds=0.2)
    run.stop()
    pump(rec.done, 5)
    pump(lambda: all(w.isFinished() for w in local_engine._threads), 3)
    nothing_new("Stop, after a step that ran", rec.result().get("stopped") is True
                and App.ActiveDocument.getObject("E2") is not None, rec.events)

    srv.add(box("E3"))
    rec = run_once(one, "more", dict(cfg, max_steps=1))
    nothing_new("the step limit", rec.result().get("incomplete") is True and "step limit" in rec.result()["result"],
                rec.events)

    srv.add(box("E4"), cut(answer("Now I add the four ho")))
    rec = run_once(one, "more", cfg)
    nothing_new("an answer that was cut off", rec.result().get("incomplete") is True
                and "cut off" in rec.result()["result"], rec.events)

    srv.add(box("E5"), call("run_python", {"code": "1 / 0"}, "bad"), answer("Done."), answer("Done."), answer("It is done."))
    rec = run_once(one, "more", cfg)
    nothing_new("a problem that the model did not repair", rec.result().get("incomplete") is True
                and "still shows a problem" in rec.result()["result"], rec.events)

    answers, real_request = [False, True], approval.request
    approval.request = lambda tool_name, args, source: answers.pop(0)
    try:
        srv.add(box("NotWanted"), box("E6"), answer("One box, the other one you did not want."))
        rec = run_once(one, "two boxes", cfg)
    finally:
        approval.request = real_request
    nothing_new("a step that the user answered with Skip (the next step ran, and the model was saved)",
                rec.result().get("is_error") is False and rec.saved() and not answers
                and App.ActiveDocument.getObject("NotWanted") is None
                and App.ActiveDocument.getObject("E6") is not None, rec.events)

    srv.add(answer("It has six boxes."))
    rec = run_once(one, "what is in the model?", cfg)
    nothing_new("a question alone: no model was saved", rec.result().get("is_error") is False and not rec.saved(),
                rec.events)

    # ---- 4: an error that the model repaired ------------------------------------------------
    say("--- 4 a tool error that the model repaired: the request is written whole")
    srv.add(call("run_python", {"code": 'doc.addObject("Part::Box", "Half")\n1 / 0'}, "r1"),
            box("Repaired"), answer("The box is there now."))
    rec = run_once(one, "one more box", cfg)
    got = rows(one)
    last = got[-1]["messages"] if got else []
    mine = [m for m in last if not m.get("context")]
    check("the request is the third line: the failed call, its error, the repair and the answer",
          len(got) == 3 and rec.saved() and [m["role"] for m in mine] == ["user", "assistant", "tool", "assistant", "tool",
                                                                          "assistant"]
          and "ZeroDivisionError" in mine[2]["content"] and mine[2]["content"].startswith("ERROR")
          and "1 / 0" in mine[1]["tool_calls"][0]["function"]["arguments"]
          and "+ new Repaired" in mine[4]["content"] and mine[5]["content"] == "The box is there now.",
          str(mine)[:500])
    check("the requests that had a problem are in its context, as the model got them",
          any(m.get("context") and m.get("content") == "*Stopped.*" for m in last)
          and all(m.get("context") for m in last[:-6]), str([m.get("content") for m in last])[:300])
    close_all()

    # ---- 5: what is never in a line ----------------------------------------------------------
    say("--- 5 a key, the token of the bridge, pixels, the text of a project file, the home folder, the project")
    two = new_project("Bracket of Max")
    person = os.path.join(home, "people", "Max Mustermann")           # the home folder, for this test
    real_homes, training._homes = training._homes, lambda: training._spellings(person)
    real_token, bridge.token = bridge.token, lambda: "BRIDGE-TOKEN-made-up"
    backends.set_api_key("sk-ant-SAVED-KEY-made-up")
    backends.set_local_key("LOCAL-KEY-made-up")
    table_lines = ["# Thread table of the school book, page 212",
                   "M8   pitch 1.25   core hole 6.8   clearance hole 9.0 (medium)",
                   "M10  pitch 1.5    core hole 8.5   clearance hole 11.0 (medium)"]
    with open(os.path.join(two, "refs", "table.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(table_lines) + "\n")
    sketch = os.path.join(two, "refs", "sketch.png")
    drawn = QtGui.QImage(64, 48, QtGui.QImage.Format_RGB32)
    drawn.fill(QtGui.QColor(200, 200, 200))
    painter = QtGui.QPainter(drawn)
    painter.fillRect(8, 8, 30, 12, QtGui.QColor(20, 20, 20))
    painter.end()
    drawn.save(sketch, "PNG")
    with open(sketch, "rb") as f:
        file_pixels = base64.b64encode(f.read()).decode("ascii")
    keys = ["sk-ant-SAVED-KEY-made-up", "LOCAL-KEY-made-up", "REQUEST-KEY-made-up", "BRIDGE-TOKEN-made-up"]
    code = ('import base64\n'
            'b = doc.addObject("Part::Box", "Bracket")\n'
            'print(PROJECT)\n'
            'print(%r)\n'
            'print("%s")\n'
            'print("data:image/png;base64," + base64.b64encode(open(PROJECT + "/refs/sketch.png", "rb").read()).decode())'
            % (os.path.join(person, "Desktop", "notes.txt"), " ".join(keys)))
    cl = FakeAnthropic([
        claude("I read the table.", [("read_project_file", {"path": "refs/table.md"}, "toolu_1")], thinking="one"),
        claude("", [("run_python", {"code": code}, "toolu_2")], thinking="two"),
        claude("The bracket is in %s. The book says: %s" % (os.path.join(two, "model"), table_lines[1]))])
    acfg = {"kind": "anthropic", "url": cl.url, "model": "claude-fake-1", "key": "REQUEST-KEY-made-up"}
    user_yes.allow("anthropic", cl.url)
    try:
        rec = run_once(two, "Make the bracket of the sketch. My keys: %s. My folder: %s, the project: %s"
                       % (" ".join(keys), person, two), acfg, images=[sketch])
        text = (raw(two) or b"").decode("utf-8")
        got = rows(two)
        sent = json.dumps(cl.requests)
        try:
            sent_pixels = cl.requests[0]["body"]["messages"][0]["content"][0]["source"]["data"]
        except (LookupError, TypeError):
            sent_pixels = ""
        check("(setup) the request ended well, with the source 'apikey', and all of it was in the conversation",
              rec.saved() and len(got) == 1 and got[0]["source"] == "apikey" and got[0]["model"] == "claude-fake-1"
              and len(sent_pixels) > 100 and table_lines[1] in sent and "SIGNATURE-one" in sent
              and "Mustermann" in sent and "Bracket of Max" in sent and "sk-ant-SAVED-KEY-made-up" in sent,
              str(rec.events)[:600])
        msgs = got[0]["messages"] if got else []
        check("no key and no token of the bridge is in the line",
              not [k for k in keys if k in text] and "(a saved key, hidden)" in text
              and "(the token of the bridge, hidden)" in text, [k for k in keys if k in text])
        check("no pixels: the image is its path in the project and a marker, also where the code printed it",
              sent_pixels not in text and file_pixels not in text and file_pixels[:60] not in text
              and msgs[0].get("images") == ["refs/sketch.png"] and msgs[0].get("image_data") == training.NO_PIXELS
              and training.IMAGE_DATA in msgs[4]["content"], msgs[4]["content"] if len(msgs) > 4 else text[:300])
        check("the text that read_project_file returned is not there: a marker with the path and the number of characters",
              not [row for row in table_lines if row in text] and "Thread table" not in text
              and msgs[2]["content"].startswith("(read_project_file: the text of refs/table.md is not exported, ")
              and msgs[2]["content"].endswith(" characters)"), msgs[2]["content"] if len(msgs) > 2 else "")
        check("... and the line of the file that the model repeated in its answer is taken out",
              "core hole 6.8" not in text and training.QUOTED in msgs[-1]["content"], msgs[-1]["content"])
        check("the home folder is ~: the name of the user is not in the line",
              person not in text and os.path.realpath(person) not in text and "Mustermann" not in text
              and "My folder: ~," in msgs[0]["content"] and "~" in msgs[4]["content"], text[:300])
        check("the path of the project is PROJECT, in the message, in the tool result and in the answer",
              two not in text and os.path.realpath(two) not in text and "Bracket of Max" not in text
              and "the project: PROJECT" in msgs[0]["content"] and "\n  PROJECT\n" in msgs[4]["content"]
              and "The bracket is in PROJECT" in msgs[-1]["content"], msgs[-1]["content"])
        check("Claude's signed thinking blocks are not in the line, and no key that starts with '_'",
              "SIGNATURE" not in text and "_native" not in text and "thinking" not in text
              and not [k for m in msgs for k in m if k.startswith("_")])
        cl.add(claude("", [("run_python", {"code": 'doc.addObject("Part::Box", "Rib")'}, "toolu_3")]),
               claude("The rib is added."))
        rec = run_once(two, "add a rib", acfg)
        got, text = rows(two), (raw(two) or b"").decode("utf-8")
        msgs = got[1]["messages"] if len(got) == 2 else []
        context = [m for m in msgs if m.get("context")]
        check("a follow-up request: the first request is its context, marked, and the request comes after it",
              len(got) == 2 and len(context) == 6 and [m["role"] for m in msgs[6:]] == ["user", "assistant", "tool",
                                                                                         "assistant"]
              and msgs[6]["content"] == "add a rib" and not [m for m in msgs[6:] if m.get("context")], str(msgs)[:400])
        check("the context has the same markers: no text of the file, no pixels, no key, no name",
              not [row for row in table_lines if row in text] and sent_pixels not in text and file_pixels[:60] not in text
              and not [k for k in keys if k in text] and "Mustermann" not in text and "Bracket of Max" not in text
              and context[2]["content"].startswith("(read_project_file: the text of refs/table.md is not exported, ")
              and context[0].get("image_data") == training.NO_PIXELS, text[:300])
        check("the conversation file of the project still has the full text: the export changed nothing in it",
              table_lines[1] in json.dumps(local_engine._load(two)))
    finally:
        training._homes, bridge.token = real_homes, real_token
        backends.set_api_key("")
        backends.set_local_key("")
    check("the Anthropic script was used exactly", cl.overrun == 0 and not cl.script, (cl.overrun, len(cl.script)))
    cl.close()
    close_all()

    # ---- 6: links, and a file that cannot be written -----------------------------------------------
    say("--- 6 a link in place of the folder or of the file; a failed write does not break the request")
    outside = os.path.join(home, "outside")
    os.makedirs(os.path.join(outside, "folder"))
    target = os.path.join(outside, "notes.txt")
    with open(target, "wb") as f:
        f.write(b"the user's own file outside the project\n")

    def link(to, name, folder=False):
        try:
            os.symlink(to, name, target_is_directory=folder)
            return True
        except (OSError, NotImplementedError, AttributeError):
            return False

    notes = []

    def good_request(project, name):
        srv.add(box(name), answer("Done."))
        rec = run_once(project, "a box", cfg)
        notes.extend(t for t in rec.texts() if "training data could not be written" in t)
        ok = rec.result() == {"is_error": False, "result": "Done."} and rec.saved()
        close_all()
        return ok

    three = new_project("link-folder")
    if link(os.path.join(outside, "folder"), os.path.join(three, "training"), folder=True):
        check("training/ is a link to a folder outside the project: the request ends well, nothing is written there",
              good_request(three, "L1") and os.listdir(os.path.join(outside, "folder")) == [], str(notes))
        four = new_project("link-file")
        os.makedirs(os.path.join(four, "training"))
        link(target, os.path.join(four, "training", "requests.jsonl"))
        with open(target, "rb") as f:
            before = f.read()
        ok = good_request(four, "L2")
        with open(target, "rb") as f:
            check("requests.jsonl is a link to a file outside the project: the request ends well, that file is not changed",
                  ok and f.read() == before == b"the user's own file outside the project\n")
    else:
        say("skip  links: this system or this user cannot make a symbolic link")
    five = new_project("file-in-the-way")
    with open(os.path.join(five, "training"), "wb") as f:
        f.write(b"a file with the name of the folder")
    check("a file that cannot be written (training is a file): the request ends well and the model is saved",
          good_request(five, "L3") and os.path.isfile(os.path.join(five, "training")), str(notes))
    check("the chat said it once, in one plain line, for all of these requests",
          len(notes) == 1 and notes[0].startswith("*The training data could not be written to `training/requests.jsonl`")
          and "Traceback" not in notes[0], str(notes))

    # ---- 7: off again -------------------------------------------------------------------------------
    say("--- 7 off again: an existing file is left alone")
    training.set_enabled(False)
    kept = raw(one)
    srv.add(box("AfterOff"), answer("Done."))
    rec = run_once(one, "one more", cfg)
    check("off: the good request is not written, the file stays as it was",
          rec.saved() and raw(one) == kept and len(rows(one)) == 3, len(rows(one)))
    close_all()

    # ---- 8: the check box, and the line in the panel ---------------------------------------------------
    say("--- 8 Settings has the check box; the panel says once where the file is")
    p.SetString("LocalUrl", srv.url)
    p.SetString("LocalModel", "fake-model")
    panel_project = projects.create("Panel part")
    pan = panel.Panel()
    pump(seconds=0.2)
    pan._select(os.path.basename(panel_project), open_model=False)
    dlg = SettingsDialog(pan)
    hints = [w.text() for w in dlg.findChildren(QtWidgets.QLabel) if training.HINT in w.text()]
    check("Settings: one check box, off, with the words of training.LABEL and a hint that says what, where, and that "
          "nothing is sent",
          isinstance(dlg.training, QtWidgets.QCheckBox) and not dlg.training.isChecked()
          and dlg.training.text() == training.LABEL == "Save good requests as training data in the project"
          and len(hints) == 1 and "training/requests.jsonl in the project folder" in training.HINT
          and "Nothing is sent anywhere" in training.HINT and "Claude Code" in training.HINT, str(hints))
    dlg.training.click()
    dlg.reject()
    check("Settings: a click and Cancel changes nothing", training.enabled() is False)
    dlg = SettingsDialog(pan)
    dlg.training.click()
    dlg._save()
    check("Settings: a click and OK turns the parameter TrainingData on",
          training.enabled() is True and p.GetBool("TrainingData", False) is True and p.GetString("Backend", "") == "local")
    dlg = SettingsDialog(pan)
    check("Settings: the check box shows the parameter", dlg.training.isChecked())
    dlg.reject()

    def send(text):
        pan.input.setPlainText(text)
        pan.send()
        return pump(lambda: pan.send_btn.text() == "Send" and not pan._running(), 30)

    srv.add(box("P1"), answer("A box."))
    done = send("make a box")
    lines = [t for k, t in pan.entries if "training" in t]
    check("the panel: the request is saved as training data, and the chat says so in one line",
          done and len(rows(panel_project)) == 1 and lines == [SAVED] and ("claude", SAVED) in pan.entries,
          str(pan.entries))
    srv.add(box("P2"), answer("Another box."))
    done = send("one more")
    check("the panel: the next good request is written too, and the line is not said again",
          done and len(rows(panel_project)) == 2 and [t for k, t in pan.entries if "training" in t] == [SAVED],
          str(pan.entries))
    dlg = SettingsDialog(pan)
    dlg.training.click()
    dlg._save()
    srv.add(box("P3"), answer("A third box."))
    done = send("and one more")
    check("the panel: the check box off again: the next request is not written",
          done and training.enabled() is False and len(rows(panel_project)) == 2
          and ("claude", "A third box.") in pan.entries, str(pan.entries[-4:]))

    check("the script of the model server was used exactly", srv.overrun == 0 and not srv.script,
          (srv.overrun, len(srv.script)))
    local_engine._shutdown()
    srv.close()
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
