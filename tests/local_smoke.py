# RUN: manual
"""One run of the local engine with a real model, in a FreeCAD GUI without a display:

    FREECAD_USER_HOME=<fresh folder> QT_QPA_PLATFORM=offscreen freecad tests/local_smoke.py

Needs an OpenAI-compatible server (LM Studio) at CADPROMPT_SMOKE_URL with the
model CADPROMPT_SMOKE_MODEL. It shows what the model did; a wrong part is the
model's fault, a run that does not end cleanly is ours. Not a pass/fail test
of the CAD result. The report goes to stdout and to
<FREECAD_USER_HOME>/local_smoke.log.
"""

import json
import os
import shutil
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

from cadprompt import local_agent, local_engine, local_tools    # noqa: E402

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
_message = local_agent.Agent._message


def _spy(self, status, raw):
    try:
        data = json.loads(raw)
        msg = data["choices"][0]["message"]
        say("   server: HTTP %d, usage %s, finish %s, reasoning %d chars, content %d chars, %d tool calls" % (
            status, json.dumps(data.get("usage")), data["choices"][0].get("finish_reason"),
            len(msg.get("reasoning_content") or ""), len(msg.get("content") or ""), len(msg.get("tool_calls") or [])))
    except Exception:
        say("   server: HTTP %d, %s" % (status, raw[:300]))
    return _message(self, status, raw)


local_agent.Agent._message = _spy


def main():
    project = os.path.join(HOME, "projects", "smoke")
    shutil.copytree(os.path.join(ADDON, "template"), project)
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    say("model %s at %s" % (MODEL, URL))
    say("prompt: %s" % PROMPT)

    events = []
    run = local_engine.LocalRun()
    run.text.connect(lambda t: (events.append("text"), say("TEXT  " + t)))
    run.tool.connect(lambda n, d: (events.append("tool:" + n), say("TOOL  %s  %s" % (n, d))))
    run.tool_error.connect(lambda n: (events.append("tool_error"), say("TOOL ERROR  %s" % n)))
    done = []
    run.finished.connect(lambda r: (done.append(dict(r)), say("FINISHED  %r" % (dict(r),))))

    # show the code the model sends and what it gets back
    real = local_tools.run_tool

    def shown(project, name, args):
        say("   >>> %s %s" % (name, json.dumps(args)[:1500]))
        out = real(project, name, args)
        say("   <<< " + out.replace("\n", "\n       "))
        return out
    local_tools.run_tool = shown

    ticks = [0]
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start(100)
    run.start(project, PROMPT, {"url": URL, "model": MODEL, "key": ""})
    end = time.time() + LIMIT
    while not done and time.time() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    if not done:
        say("NOT FINISHED after %.0f s: calling stop()" % LIMIT)
        run.stop()
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
    say("document state:\n" + local_tools.get_document_state())
    model_dir = os.path.join(project, "model")
    say("files in model/: %r" % (sorted(os.listdir(model_dir)) if os.path.isdir(model_dir) else None))
    say("history: %r" % [(k, t[:80]) for k, t in local_engine.history(project)])
    clean = len(done) == 1 and not run.running()
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
