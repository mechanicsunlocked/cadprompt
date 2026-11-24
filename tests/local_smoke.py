# RUN: manual
"""One message to a real local model through the real panel, in a FreeCAD GUI without a display:

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/local_smoke.py

Needs an OpenAI-compatible server (LM Studio) at CADPROMPT_SMOKE_URL with the
model CADPROMPT_SMOKE_MODEL. The settings are those of a new installation,
with the local model chosen: "Run the AI's code without asking" is off. So
each piece of code of the model waits in the panel; this script is the user
who reads it and presses Run. A server on this computer needs no question
about the service; for another address the script says Allow.

It shows what the model did; a wrong part is the model's fault, a run that
does not end cleanly is ours. Not a pass/fail test of the CAD result. The
report goes to stdout and to <FREECAD_USER_HOME>/local_smoke.log.
"""

import json
import os
import sys
import threading
import time
import traceback

if not os.environ.get("FREECAD_USER_HOME"):
    os.write(1, b"Set FREECAD_USER_HOME to a fresh folder first.\n")
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
ADDON = os.path.dirname(HERE)
sys.path.insert(0, ADDON)

import FreeCAD as App                                   # noqa: E402
from PySide import QtCore, QtWidgets                    # noqa: E402

# see test_local_engine.py: FreeCAD 1.1 deadlocks on this Qt warning without a display
QtCore.QLoggingCategory.setFilterRules("default.warning=false")

from cadprompt import approval, ask_ui, consent, local_agent, local_engine, local_tools, panel, paths, projects    # noqa: E402

URL = os.environ.get("CADPROMPT_SMOKE_URL", "http://127.0.0.1:1234/v1")
MODEL = os.environ.get("CADPROMPT_SMOKE_MODEL", "google/gemma-4-e4b")
PROMPT = os.environ.get("CADPROMPT_SMOKE_PROMPT", "Make a plate 100 x 50 x 10 mm.")
LIMIT = float(os.environ.get("CADPROMPT_SMOKE_SECONDS", "240"))
HOME = os.environ["FREECAD_USER_HOME"]
LOG = os.path.join(HOME, "local_smoke.log")
open(LOG, "w", encoding="utf-8").close()
T0 = time.time()


def say(line):
    line = "[%5.1f s] %s" % (time.time() - T0, line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        os.write(1, (line + "\n").encode("utf-8"))
    except OSError:
        pass


# show what the server used, for the report only
_message = local_agent.OpenAIWire.answer


def _spy(self, status, headers, raw, pictures):
    try:
        data = json.loads(raw)
        msg = data["choices"][0]["message"]
        say("   server: HTTP %d, usage %s, finish %s, reasoning %d chars, content %d chars, %d tool calls" % (
            status, json.dumps(data.get("usage")), data["choices"][0].get("finish_reason"),
            len(msg.get("reasoning_content") or ""), len(msg.get("content") or ""), len(msg.get("tool_calls") or [])))
    except Exception:
        say("   server: HTTP %d, %s" % (status, raw[:300]))
    return _message(self, status, headers, raw, pictures)


local_agent.OpenAIWire.answer = _spy


def main():
    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(HOME, "projects"))
    p.SetString("Backend", "local")
    p.SetString("LocalUrl", URL)
    p.SetString("LocalModel", MODEL)
    projects.create("smoke")
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    say("model %s at %s" % (MODEL, URL))
    say("prompt: %s" % PROMPT)
    say("run without asking: %r; the server is on this computer: %r" % (approval.auto(), consent.on_this_computer(URL)))

    panel.show()                             # the real dock
    pan = panel._dock.widget()
    events, done, asked, ran = [], [], [], []
    run = pan._local_run()
    run.text.connect(lambda t: (events.append("text"), say("TEXT  " + t)))
    run.tool.connect(lambda n, d: (events.append("tool:" + n), say("TOOL  %s  %s" % (n, d))))
    run.tool_error.connect(lambda n: (events.append("tool_error"), say("TOOL ERROR  %s" % n)))
    run.finished.connect(lambda r: (done.append(dict(r)), say("FINISHED  %r" % (dict(r),))))

    # show the code the model sends and what it gets back
    real = local_tools.run_tool

    def shown(project, name, args):
        say("   >>> %s %s" % (name, json.dumps(args)[:1500]))
        ran.append((name, approval.shown(name, args) if name in ("run_python", "make_drawing") else None))
        out = real(project, name, args)
        say("   <<< " + out.replace("\n", "\n       "))
        return out
    local_tools.run_tool = shown

    ticks, state = [0], {"since": None, "services": 0}

    def tick():
        """The user: reads a question in the panel and presses Run after a moment."""
        ticks[0] += 1
        for w in QtWidgets.QApplication.topLevelWidgets():       # a server on another computer
            if isinstance(w, ask_ui.Question) and not w.isHidden() and w.answer is None:
                say("QUESTION  %s -> Allow" % w.headline.text())
                state["services"] += 1
                w.yes_btn.click()
        if not pan.asking():
            state["since"] = None
            return
        if state["since"] is None:
            state["since"] = time.time()
            objects = [o.Name for o in App.ActiveDocument.Objects] if App.ActiveDocument else []
            before = len([1 for _name, text in ran if text is not None])
            asked.append((pan.card.title.text(), pan.card.shown_text(), before, objects))
            say("APPROVAL  %s  (on screen: %r, status: %r, ran so far: %d, objects: %r)\n          | %s" % (
                pan.card.title.text(), pan.card.isVisible(), pan.status.text(), len(ran), objects,
                pan.card.shown_text().replace("\n", "\n          | ")))
        elif time.time() - state["since"] >= 1.0:
            say("          -> Run")
            pan.card.run_btn.click()
    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(100)

    pan.input.setPlainText(PROMPT)
    pan.send()
    end = time.time() + LIMIT
    while not done and time.time() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    if not done:
        say("NOT FINISHED after %.0f s: calling stop()" % LIMIT)
        pan._stop()
    end = time.time() + 5
    while time.time() < end and not all(w.isFinished() for w in local_engine._threads):
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)

    say("")
    say("=== summary ===")
    say("events: %s" % " ".join(events))
    say("finished emitted %d time(s): %r" % (len(done), done))
    say("running() afterwards: %r; worker threads still alive: %d" % (
        run.running(), len([w for w in local_engine._threads if not w.isFinished()])))
    say("GUI timer ticks during the run (100 ms each): %d in %.0f s" % (ticks[0], time.time() - T0))
    say("questions about a service: %d; questions 'Run or Skip': %d" % (state["services"], len(asked)))
    changing = [text for _name, text in ran if text is not None]
    exact = [text for _title, text, _n, _objects in asked] == changing
    waited = all(before == i for i, (_title, _text, before, _objects) in enumerate(asked))
    say("every step that changes things was shown first, and ran exactly as shown: %r" % exact)
    say("no step ran while its question was open: %r" % waited)
    say("panel entries:")
    for kind, text in pan.entries:
        say("   [%s] %s" % (kind, text.replace("\n", "\n      ")[:600]))
    say("document state:\n" + local_tools.get_document_state())
    model_dir = os.path.join(pan.project, "model")
    say("files in model/: %r" % (sorted(os.listdir(model_dir)) if os.path.isdir(model_dir) else None))
    clean = len(done) == 1 and not run.running() and exact and waited
    say("LOOP ENDED CLEANLY" if clean else "LOOP DID NOT END CLEANLY")
    return 0 if clean else 1


def finish():
    code = 1
    try:
        code = main()
    except Exception:
        say("EXCEPTION in the smoke script:\n" + traceback.format_exc())
    os._exit(code)


def watchdog():
    say("WATCHDOG: the GUI thread hung")
    os._exit(3)


threading.Timer(LIMIT + 40, watchdog).start()
QtCore.QTimer.singleShot(500, finish)
