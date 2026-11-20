# RUN: manual
"""How often does a local model build the right part? 20 build requests, checked by numbers.

    FREECAD_USER_HOME=<fresh folder> freecadcmd tests/local_bench.py

Needs an OpenAI-compatible server (LM Studio, Ollama, llama-server) with the
model loaded or ready to load. FreeCAD runs without a window. The options are
environment variables, not arguments: freecadcmd takes every word after the
script for a file to open, on all three systems.

    CADPROMPT_BENCH_URL       address of the server (default http://127.0.0.1:1234/v1)
    CADPROMPT_BENCH_MODELS    model names, with commas between them (default google/gemma-4-e4b)
    CADPROMPT_BENCH_KEY       API key of the server, when it wants one. Never written to a file.
    CADPROMPT_BENCH_PASSES    how often the whole list is run (default 1)
    CADPROMPT_BENCH_ONLY      a selection: ids or parts of ids, with commas ("01,flange")
    CADPROMPT_BENCH_OUT       folder for the results (default <FREECAD_USER_HOME>/bench)
    CADPROMPT_BENCH_SECONDS   the longest that one request may take (default 900)

What runs is the built-in engine as the panel uses it for a local model
(local_engine.LocalRun): the system prompt of local_prompt, its tool list, the
tools of local_tools, the step limit and the time limits of local_agent.
Nothing of that is copied here, so a change of the prompt or of what
run_python offers shows in the numbers.

For each request: all documents closed, the variables of run_python forgotten,
a new project folder, a new empty document, the request as one message. This
script is the user: each time the engine asks "Run or Skip" for a step, it
says Run (it is the panel that approval.request asks; no setting is changed).
A server on this computer needs no question about the service; for another
address the script says Allow in its scratch profile. Then bench_checks.py
measures the part and compares it with the numbers of bench_requests.py.

A sample passes when the request ended well for the engine (no error, not the
step limit, no problem left) and every check is right. Because a model does
not answer the same way twice, run several passes: the rate is the passed
samples of all samples, and the difference between the passes is the noise.

Not the same as in the panel: FreeCAD has no window here, so `Gui` is not
there and `obj.ViewObject` is None, and code that uses them fails.

For each model the table goes to stdout (between FreeCAD's own progress
lines) and to <out>/<model>.txt; everything, with each conversation, goes to
<out>/<model>.json, and the document of each request to <out>/<model>-models/.
The exit code is 0 when the run was complete.
"""

import datetime
import json
import os
import re
import sys
import threading
import time
import traceback

if not os.environ.get("FREECAD_USER_HOME"):
    sys.stdout.write("Set FREECAD_USER_HOME to a fresh folder first.\n")
    sys.stdout.flush()
    os._exit(2)

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.join(os.getcwd(), "tests")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

import FreeCAD as App                           # noqa: E402
from PySide import QtCore                       # noqa: E402

from cadprompt import (approval, backends, consent, local_agent, local_engine, local_prompt,    # noqa: E402
                       local_tools, paths, projects, time_limit)
import bench_checks                             # noqa: E402
import user_yes                                 # noqa: E402
from bench_requests import GROUPS, REQUESTS     # noqa: E402

HOME = os.environ["FREECAD_USER_HOME"]
CHECK_SECONDS = 120          # the longest that the measuring of one part may take
TOOL_NAMES = tuple(t["function"]["name"] for t in local_prompt.TOOLS)
_answers = []                # the "usage" of every answer of the server to the request that runs
_key = [""]                  # the API key of this run: taken out of everything that is written
_alive = [time.monotonic()]  # the last time that the main thread came back from FreeCAD


class CheckTooLong(Exception):
    pass


class User:
    """The user of this run: the engine asks "Run or Skip", and the answer is Run."""

    def __init__(self):
        self.asked = 0

    def asks(self):
        return True

    def ask_approval(self, tool_name, args, source):
        self.asked += 1
        return True

    def withdraw(self):
        pass


def _counted(real):
    def answer(self, status, headers, raw, pictures):
        try:
            usage = json.loads(raw).get("usage") or {}
        except (ValueError, AttributeError):
            usage = {}
        _answers.append(usage if isinstance(usage, dict) else {})
        return real(self, status, headers, raw, pictures)
    return answer


def setup():
    """Once: the Qt application that the engine's signals need, the user, the counter."""
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication(sys.argv[:1])
    user = User()
    approval.register(user)
    if not getattr(local_agent.OpenAIWire.answer, "_counts", False):
        local_agent.OpenAIWire.answer = _counted(local_agent.OpenAIWire.answer)
        local_agent.OpenAIWire.answer._counts = True
    paths.params().SetString("ProjectsDir", os.path.join(HOME, "projects"))
    return app, user


def engine_config(url, model, key=""):
    """The settings of a user who chose the local model, and what the engine gets for them."""
    p = paths.params()
    url = consent.with_scheme(url.strip().rstrip("/"), bool(key))
    p.SetString("Backend", "local")
    p.SetString("LocalUrl", url)
    p.SetString("LocalModel", model)
    if not consent.needs_none("openai", url):
        user_yes.allow("openai", url)        # the person who named this address said Allow
    config = backends.engine_config("local")
    if key:
        config["key"] = key                  # in memory only: the profile of this run keeps no key
    return config


def warm_up(config):
    """One small question before the clock starts, so that the first request does not pay
    for the loading of the model. Returns "" or why the model cannot be used."""
    agent = local_agent.Agent(config["url"], config["model"], lambda name, args: "", key=config.get("key", ""),
                              may_contact=backends.may_contact)
    try:
        agent.ask("Answer with the one word: ready")
    except local_agent.AgentError as exc:
        return str(exc)
    return ""


def close_all():
    for name in list(App.listDocuments()):
        App.closeDocument(name)


def fresh_project(name):
    """A new project folder from the template: never one that has a conversation already."""
    stem, n = name, 1
    while os.path.exists(projects.path_of(projects.slug(name))):
        n += 1
        name = "%s-%d" % (stem, n)
    return projects.create(name)


def watch(seconds):
    """End the program when the main thread does not come back from FreeCAD: a shape that
    FreeCAD computes for ever would stop the whole run, and the results so far are on disk."""
    def look():
        while True:
            time.sleep(5)
            if time.monotonic() - _alive[0] > seconds:
                sys.stdout.write("\nThe benchmark stopped: FreeCAD did not come back for %d seconds.\n" % seconds)
                sys.stdout.flush()
                os._exit(3)
    thread = threading.Thread(target=look, daemon=True)
    thread.start()


def _ending(res, timed_out, limit):
    """Why the engine did not end a request well, or ""."""
    if timed_out:
        return "time limit of the benchmark (%d s)" % limit
    text = str(res.get("result") or "")
    if res.get("is_error"):
        return "engine error: " + " ".join(text.split())[:160]
    if not res.get("incomplete"):
        return ""
    if text.startswith(local_agent.STEP_LIMIT.split("%")[0]):
        return "step limit (%s)" % "".join(re.findall(r"\((\d+)\)", text)[:1])
    if local_agent.CUT_OFF in text:
        return "the answer was cut off (token limit)"
    if local_agent.STILL_WRONG in text:
        return "ended with a problem that the model did not repair"
    return "the request did not end well"


def measure_and_check(req, doc):
    """(checks, the measured numbers as data). Never raises: a part that cannot be measured fails."""
    try:
        with time_limit.Limit(CHECK_SECONDS, CheckTooLong):
            m = bench_checks.measure(doc)
            return bench_checks.check(req, m), bench_checks.summary(m)
    except Exception as exc:
        text = "the part could not be measured: %s" % (str(exc) or exc.__class__.__name__)
        return [{"check": "measure", "ok": False, "found": text, "wanted": "a part that can be measured"}], None


def keep_model(doc, folder, name):
    """A copy of the document as the request left it, to look at in FreeCAD. Returns its file name."""
    if doc is None or not folder:
        return ""
    try:
        os.makedirs(folder, exist_ok=True)
        doc.saveCopy(os.path.join(folder, name + ".FCStd"))
    except Exception:
        return ""
    return name + ".FCStd"


def run_request(app, req, config, limit, name, models=""):
    """One request through the engine, from an empty document to the checked part.
    `models`: the folder that gets a copy of the document, whatever the end of the request."""
    close_all()
    local_tools.reset()
    project = fresh_project(name)
    doc = App.newDocument("Model")
    doc.UndoMode = 1         # as in FreeCAD's window; without it a run that failed is not taken back
    del _answers[:]
    done, tools, errors = [], [], []
    run = local_engine.LocalRun()
    run.tool.connect(lambda tool, detail: tools.append(tool))
    run.tool_error.connect(lambda tool: errors.append(tool))
    run.finished.connect(lambda res: done.append(dict(res)))
    began = time.monotonic()
    run.start(project, req["text"], config)
    while not done and time.monotonic() - began < limit:
        app.processEvents()
        _alive[0] = time.monotonic()
        time.sleep(0.01)
    seconds = time.monotonic() - began
    timed_out = not done
    if timed_out:
        run.stop()
    end = time.monotonic() + 10
    while time.monotonic() < end and not all(w.isFinished() for w in local_engine._threads):
        app.processEvents()
        time.sleep(0.01)
    res = done[-1] if done else {}
    reason = _ending(res, timed_out, limit)
    doc = App.listDocuments().get(run._doc or "") or App.ActiveDocument
    _alive[0] = time.monotonic()
    checks, part = measure_and_check(req, doc)
    _alive[0] = time.monotonic()
    failure = bench_checks.first_failure(checks)
    if reason:
        reason += "; the part passes the checks" if not failure else "; " + failure
    sample = {"id": req["id"], "group": req["group"], "ok": not reason and not failure,
              "reason": reason or failure, "turns": len(_answers),
              "tool_calls": len([t for t in tools if t in TOOL_NAMES]), "tool_errors": len(errors),
              "seconds": round(seconds, 1), "part_ok": not failure, "ended_well": not reason,
              "prompt_tokens": [u.get("prompt_tokens") for u in _answers][:1],
              "completion_tokens": sum(u.get("completion_tokens") or 0 for u in _answers),
              "checks": checks, "part": part, "answer": str(res.get("result") or ""),
              "state": local_tools.get_document_state(), "model_file": keep_model(doc, models, name),
              "messages": local_engine._load(project)}
    close_all()
    return sample


# ---- the report ------------------------------------------------------------------
def _rate(samples):
    good = len([s for s in samples if s["ok"]])
    return "%d of %d (%d %%)" % (good, len(samples), round(100.0 * good / len(samples))) if samples else "0 of 0"


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _steps(samples):
    return "mean %.1f model turns, %.1f tool calls, %.0f s" % (
        _mean(s["turns"] for s in samples), _mean(s["tool_calls"] for s in samples),
        _mean(s["seconds"] for s in samples))


def row(sample, width):
    return "%-*s  %-6s  %-4s  %5d  %5d  %7.1f  %s" % (
        width, sample["id"], sample["group"], "pass" if sample["ok"] else "FAIL", sample["turns"],
        sample["tool_calls"], sample["seconds"], sample["reason"])


def head(width):
    return "%-*s  %-6s  %-4s  %5s  %5s  %7s  %s" % (width, "request", "group", "", "turns", "calls", "seconds",
                                                    "the check that failed (found; wanted)")


def totals(samples, passes):
    """The lines under the table: the rate of all samples, of each pass, of each group, of each request."""
    lines = ["pass rate: %s    %s" % (_rate(samples), _steps(samples))]
    if passes > 1:
        counts = [len([s for s in samples if s["pass"] == p and s["ok"]]) for p in range(1, passes + 1)]
        lines.append("each pass: " + ", ".join("%d of %d" % (c, len([s for s in samples if s["pass"] == p]))
                                               for p, c in enumerate(counts, 1))
                     + "    difference between the passes: %d" % (max(counts) - min(counts)))
    for group in GROUPS:
        mine = [s for s in samples if s["group"] == group]
        if mine:
            lines.append("group %-6s: %s" % (group, _rate(mine)))
    if passes > 1:
        ids = []
        for s in samples:
            if s["id"] not in ids:
                ids.append(s["id"])
        lines.append("each request, passed of %d: " % passes + "  ".join(
            "%s %d" % (i.split("-")[0], len([s for s in samples if s["id"] == i and s["ok"]])) for i in ids))
    shown = len([s for s in samples if s["part"] and s["part"]["shown_inputs"]])
    if shown:
        lines.append("note: in %d samples an input of a fillet, chamfer or mirror is still switched on; "
                     "FreeCAD's window shows it over the result" % shown)
    return lines


def summary(samples, passes):
    counts = [len([s for s in samples if s["pass"] == p and s["ok"]]) for p in range(1, passes + 1)]
    return {"passed": len([s for s in samples if s["ok"]]), "samples": len(samples), "each_pass": counts,
            "groups": {g: {"passed": len([s for s in samples if s["group"] == g and s["ok"]]),
                           "samples": len([s for s in samples if s["group"] == g])} for g in GROUPS},
            "requests": {i: len([s for s in samples if s["id"] == i and s["ok"]])
                         for i in dict.fromkeys(s["id"] for s in samples)},
            "mean_turns": round(_mean(s["turns"] for s in samples), 2),
            "mean_tool_calls": round(_mean(s["tool_calls"] for s in samples), 2),
            "mean_seconds": round(_mean(s["seconds"] for s in samples), 1)}


def _clean(text):
    return text.replace(_key[0], "(the key)") if _key[0] else text


def write_json(path, data):
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(_clean(json.dumps(data, indent=1, ensure_ascii=False)) + "\n")
    os.replace(path + ".tmp", path)


def run_model(app, url, model, key, passes, requests, limit, out):
    """All passes for one model. Returns the samples."""
    stem = os.path.join(out, re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "model")
    open(stem + ".txt", "w", encoding="utf-8").close()

    def say(line=""):
        line = _clean(line)
        with open(stem + ".txt", "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    config = engine_config(url, model, key)
    version = App.Version()
    report = {"benchmark": "cadprompt local_bench", "model": model, "url": config["url"], "passes": passes,
              "requests": [r["id"] for r in requests], "started": datetime.datetime.now().isoformat(timespec="seconds"),
              "freecad": "%s.%s.%s" % (version[0], version[1], version[2]),
              "system_prompt_characters": len(local_prompt.system_prompt("build")),
              "step_limit": local_agent.OpenAIWire.max_steps, "seconds_limit": limit, "samples": []}
    say()
    say("model %s at %s: %d pass%s of %d requests" % (model, config["url"], passes, "" if passes == 1 else "es",
                                                     len(requests)))
    say("system prompt %d characters, step limit %d, FreeCAD %s" % (
        report["system_prompt_characters"], report["step_limit"], report["freecad"]))
    problem = warm_up(config)
    if problem:
        say("This model was not run: " + problem)
        report["problem"] = problem
        write_json(stem + ".json", report)
        return None
    width = max(len(r["id"]) for r in requests)
    samples = report["samples"]
    began = time.monotonic()
    for number in range(1, passes + 1):
        say()
        say("pass %d" % number)
        say(head(width))
        for req in requests:
            name = "%s-p%d-%s" % (os.path.basename(stem), number, req["id"])
            try:
                sample = run_request(app, req, config, limit, name, stem + "-models")
            except Exception:
                sample = {"id": req["id"], "group": req["group"], "ok": False, "turns": 0, "tool_calls": 0,
                          "tool_errors": 0, "seconds": 0.0, "part": None, "part_ok": False, "ended_well": False,
                          "checks": [], "messages": [],
                          "reason": "the benchmark itself failed: " + traceback.format_exc(limit=-2).strip()}
                close_all()
            sample["pass"] = number
            samples.append(sample)
            say(row(sample, width))
            report["summary"] = summary(samples, number)
            write_json(stem + ".json", report)          # after every sample: a run that is stopped keeps its results
        if passes > 1:
            say("pass %d: %s" % (number, _rate([s for s in samples if s["pass"] == number])))
    report["minutes"] = round((time.monotonic() - began) / 60, 1)
    say()
    for line in totals(samples, passes):
        say(line)
    say("time for all of it: %.1f minutes" % report["minutes"])
    write_json(stem + ".json", report)
    return samples


def selected(only):
    words = [w.strip() for w in (only or "").split(",") if w.strip()]
    return [r for r in REQUESTS if not words or any(w in r["id"] for w in words)]


def main():
    env = os.environ
    url = env.get("CADPROMPT_BENCH_URL") or backends.LOCAL_URL
    models = [m.strip() for m in (env.get("CADPROMPT_BENCH_MODELS") or "google/gemma-4-e4b").split(",") if m.strip()]
    # out of the environment: the code of the model runs in this process
    _key[0] = key = (env.pop("CADPROMPT_BENCH_KEY", "") or "").strip()
    passes = max(1, int(env.get("CADPROMPT_BENCH_PASSES") or 1))
    limit = float(env.get("CADPROMPT_BENCH_SECONDS") or 900)
    out = env.get("CADPROMPT_BENCH_OUT") or os.path.join(HOME, "bench")
    requests = selected(env.get("CADPROMPT_BENCH_ONLY"))
    if not requests:
        sys.stdout.write("CADPROMPT_BENCH_ONLY matches no request. The ids: %s\n" % ", ".join(r["id"] for r in REQUESTS))
        return 2
    os.makedirs(out, exist_ok=True)
    app, _user = setup()
    watch(limit + 600)
    complete = True
    for model in models:
        complete = run_model(app, url, model, key, passes, requests, limit, out) is not None and complete
    sys.stdout.write("\nThe results are in %s\n" % out)
    return 0 if complete else 1


def started():
    """True when FreeCAD was started with this file. The name of the module does not tell:
    FreeCAD loads a script as a module with the name of its file, and
    test_local_bench.py imports this file for its functions."""
    return any(os.path.basename(arg) == "local_bench.py" for arg in sys.argv[1:])


if started():
    code = 1
    try:
        code = main()
    except Exception:
        sys.stdout.write("The benchmark stopped:\n" + traceback.format_exc())
    sys.stdout.flush()
    os._exit(code)
