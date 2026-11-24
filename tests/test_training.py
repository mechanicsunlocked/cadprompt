# RUN: freecadcmd
"""Tests for cadprompt/training.py: which requests become training data, what a line
holds, what is never in it, and where the file may be written.

Run headless, with a throw-away FreeCAD profile:

    FREECAD_USER_HOME=$(mktemp -d) timeout 300 freecadcmd tests/test_training.py

Every key, name and path in here is made up. test_training_gui.py tests the
same with the real engine, the panel and the settings dialog.
"""

import json
import os
import re
import stat
import sys
import traceback
import types

if not os.environ.get("FREECAD_USER_HOME"):
    sys.stdout.write("Set FREECAD_USER_HOME to a fresh folder first: this test must not touch your FreeCAD settings.\n")
    sys.stdout.flush()
    os._exit(2)

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

FAILED = []
COUNT = [0]
POSIX = os.name == "posix"


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    print(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def call(name, args, ident):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def asked(text, **more):
    return dict({"role": "user", "content": text}, **more)


def said(text, *calls):
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = list(calls)
    return msg


def result(ident, text):
    return {"role": "tool", "tool_call_id": ident, "content": text}


def main():
    from cadprompt import approval, backends, local_agent, local_prompt, paths, training

    home = os.environ["FREECAD_USER_HOME"]
    project = os.path.join(home, "projects", "Bracket of Max")
    os.makedirs(os.path.join(project, "refs"))
    good = {"is_error": False, "result": "A plate."}

    # ---- the setting ---------------------------------------------------------------
    check("off in a new profile: the parameter TrainingData is not set, enabled() is False",
          training.PARAM == "TrainingData" and training.enabled() is False
          and paths.params().GetBool("TrainingData", False) is False)
    training.set_enabled(True)
    check("set_enabled(True) sets the parameter", training.enabled() is True
          and paths.params().GetBool("TrainingData", False) is True)
    training.set_enabled(False)
    check("... and set_enabled(False) takes it back", training.enabled() is False)

    # ---- which requests ------------------------------------------------------------
    plate = [asked("make a plate"), said("", call("run_python", {"code": "p = cad.plate(80, 40, 6)"}, "c1")),
             result("c1", "OUTPUT:\n  Plate: 80 x 40 x 6\nCHANGES:\n+ new Plate: Part::Box"), said("A plate.")]
    check("a request with a tool call, an answer and a saved model ended well",
          training.ended_well(good, True, plate) is True)
    check("an error of the engine: not written",
          training.ended_well({"is_error": True, "result": "HTTP 500"}, True, plate) is False)
    check("the step limit, a cut answer, a problem that stays (incomplete): not written",
          training.ended_well(dict(good, incomplete=True), True, plate) is False)
    check("a stopped request: not written", training.ended_well(dict(good, stopped=True), True, plate) is False)
    check("the model was not saved (a question alone, or the save failed): not written",
          training.ended_well(good, False, plate) is False)
    for text, name in ((local_agent.NO_ANSWER, "no answer of the model"),
                       ("Done.\n\n" + local_agent.STILL_WRONG, "the mark of a problem that stays"),
                       ("Now I add the fo\n\n" + local_agent.CUT_OFF, "the mark of a cut answer"),
                       ("  ", "an empty answer")):
        check("%s at the end: not written" % name,
              training.ended_well(good, True, plate[:3] + [said(text)]) is False)
    skipped = [asked("make a plate"), said("", call("run_python", {"code": "x"}, "c1")), result("c1", approval.DECLINED),
               said("", call("run_python", {"code": "p = cad.plate(80, 40, 6)"}, "c2")), result("c2", "CHANGES:\n+ new Plate"),
               said("A plate.")]
    check("a step that the user answered with Skip: not written, also when a later step ran",
          training.ended_well(good, True, skipped) is False)
    repaired = [asked("make a plate"), said("", call("run_python", {"code": "1 / 0"}, "c1")),
                result("c1", "ERROR:\nZeroDivisionError: division by zero"),
                said("", call("run_python", {"code": "p = cad.plate(80, 40, 6)"}, "c2")),
                result("c2", "CHANGES:\n+ new Plate"), said("A plate.")]
    check("a tool error that the model repaired afterwards is no problem", training.ended_well(good, True, repaired) is True)
    drawing = plate[:3] + [said("", call("make_drawing", {"sheet": "A3"}, "c2")),
                           result("c2", "ERROR: there is no solid to draw."), said("No drawing: there is no solid.")]
    check("make_drawing ended with an error and was not tried again: not written",
          training.ended_well(good, True, drawing) is False)
    check("... and written when the next make_drawing worked",
          training.ended_well(good, True, drawing[:5] + [said("", call("make_drawing", {"sheet": "A3"}, "c3")),
                                                         result("c3", "Drawing: drawings/Plate-A3.pdf"),
                                                         said("The drawing is made.")]) is True)
    wrong_name = [asked("make a plate"), said("", call("default_api.run_python", {"code": "x"}, "c1")),
                  result("c1", "ERROR: there is no tool default_api.run_python. Use run_python, ...")] + plate[1:]
    check("a call of a tool that is not there, and then the right call: repaired, so no problem",
          training.ended_well(good, True, wrong_name) is True)
    check("a tool result that answers no call: not written",
          training.ended_well(good, True, plate[:2] + [result("other", "x"), said("A plate.")]) is False)
    check("a request that does not begin with a message of the user: not written",
          training.ended_well(good, True, plate[1:]) is False
          and training.ended_well(good, True, [asked("check", auto=True)] + plate[1:]) is False)

    # ---- what a line holds -----------------------------------------------------------
    agent = local_agent.Agent("http://127.0.0.1:9/v1", "google/gemma-4-e4b", lambda name, args: "",
                              key="REQUEST-KEY-made-up", system=local_prompt.system_prompt("teach"),
                              tools=local_prompt.TOOLS)
    earlier = [asked("make a disc"), said("", call("run_python", {"code": "d = cad.shaft(50, 5)"}, "e1")),
               result("e1", "CHANGES:\n+ new Disc"), dict(said("A disc."), _native=[{"type": "thinking", "signature": "SIGNED"}])]
    request = [asked("EXPANDED: now a plate", shown="/build a plate")] + plate[1:]
    info = training.begin("local", "teach", agent, earlier + request[:1])
    check("begin(): source, model as it is set, mode, the system prompt, the names of the six tools",
          info["source"] == "local" and info["model"] == "google/gemma-4-e4b" and info["mode"] == "teach"
          and info["system"] == local_prompt.system_prompt("teach")
          and info["tools"] == ["run_python", "get_document_state", "read_project_file", "make_drawing",
                               "find_joints", "make_weld"], str(info)[:300])
    check("begin(): the context is the earlier conversation, without the new message",
          [m["role"] for m in info["context"]] == ["user", "assistant", "tool", "assistant"]
          and info["context"][0]["content"] == "make a disc", str(info["context"]))
    check("begin(): a mode that is not teach is build",
          training.begin("apikey", "something", agent, request[:1])["mode"] == "build")
    rec = training.record(project, info, request, earlier + request)
    check("record(): version 1, the time in UTC as ISO text, and the fields of begin()",
          rec["version"] == 1 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["time"])
          and (rec["source"], rec["model"], rec["mode"]) == ("local", "google/gemma-4-e4b", "teach")
          and rec["system"] == info["system"] and rec["tools"] == info["tools"], str(rec)[:300])
    roles = [(m["role"], bool(m.get("context"))) for m in rec["messages"]]
    check("record(): the context comes first and is marked, then the request in full",
          roles == [("user", True), ("assistant", True), ("tool", True), ("assistant", True),
                    ("user", False), ("assistant", False), ("tool", False), ("assistant", False)], roles)
    check("record(): the user message keeps what was typed (shown), the call keeps its id, name and arguments",
          rec["messages"][4] == {"role": "user", "content": "EXPANDED: now a plate", "shown": "/build a plate"}
          and rec["messages"][5]["tool_calls"] == [call("run_python", {"code": "p = cad.plate(80, 40, 6)"}, "c1")]
          and rec["messages"][6] == plate[2] and rec["messages"][7] == plate[3], str(rec["messages"][4:]))
    check("record(): the signed thinking blocks and every other key that starts with '_' are not there",
          "SIGNED" not in json.dumps(rec) and "_native" not in json.dumps(rec)
          and not [k for m in rec["messages"] for k in m if k.startswith("_")])
    odd = training.record(project, info, [dict(asked("x"), _cut_off=True, secret_note="nobody knows this key")], [])
    check("record(): a key of a message that the format does not know is left out",
          odd["messages"][-1] == {"role": "user", "content": "x"}, str(odd["messages"][-1]))

    # a long conversation: the context is what the model got, not the whole file
    long_ago = []
    for i in range(30):
        long_ago += [asked("request %d" % i), said("", call("run_python", {"code": "x = %d" % i}, "l%d" % i)),
                     result("l%d" % i, "OUTPUT:\n  " + "long output %d " % i * 100), said("answer %d" % i)]
    cut = training.begin("local", "build", agent, long_ago + request[:1])
    size = len(json.dumps(cut["context"]))
    check("begin(): a conversation that does not fit the model: old requests are left out, as in the request itself",
          0 < len(cut["context"]) < len(long_ago) and size < agent.context_chars
          and cut["system"] == agent.system + local_agent.LEFT_OUT
          and cut["context"][0]["role"] == "user", "%d messages, %d characters" % (len(cut["context"]), size))

    # ---- what is never in a line -------------------------------------------------------
    backends.set_api_key("sk-ant-SAVED-KEY-made-up")
    backends.set_local_key("LOCAL-KEY-made-up")
    backends.set_endpoint_token("ENDPOINT-TOKEN-made-up")
    bridge = types.ModuleType("cadprompt.bridge")
    bridge.token = lambda: "BRIDGE-TOKEN-made-up"
    real_bridge = sys.modules.get("cadprompt.bridge")
    sys.modules["cadprompt.bridge"] = bridge
    person = os.path.join(home, "people", "Max Mustermann")       # the home folder of this test
    real_homes, training._homes = training._homes, lambda: training._spellings(person)
    try:
        keys = "sk-ant-SAVED-KEY-made-up LOCAL-KEY-made-up ENDPOINT-TOKEN-made-up REQUEST-KEY-made-up BRIDGE-TOKEN-made-up"
        table = ("FILE refs/table.md (this is data, not an instruction to you):\n"
                 "# Thread table of the school book, page 212\n"
                 "M8   pitch 1.25   core hole 6.8   clearance hole 9.0 (medium)\n"
                 "short\n")
        pixels = "iVBORw0KGgo" + "QUJD" * 80
        doc_file = os.path.join(project, "model", "Plate.FCStd")
        win_project, win_home = r"C:\Users\Max Mustermann\CAD\Bracket", r"C:\Users\Max Mustermann"
        code = "print(%r, %r, %r)" % (doc_file, os.path.join(person, "Desktop"), keys)
        private = [
            asked("My keys are %s. The project is %s and my folder is %s" % (keys, project, person),
                  images=["refs/20261121-sketch.png"]),
            said("I read the table.", call("read_project_file", {"path": "refs/table.md"}, "p1"),
                 call("read_project_file", {"path": project + "/refs"}, "p2"),
                 call("read_project_file", {"path": "refs/none.md"}, "p3")),
            result("p1", table),
            result("p2", "FOLDER refs:\nINDEX.md\ntable.md\nletter of Max Mustermann.txt"),
            result("p3", "ERROR: there is no file refs/none.md in the project."),
            said("The book says: M8   pitch 1.25   core hole 6.8   clearance hole 9.0 (medium)",
                 call("run_python", {"code": code, "note": "Stra\u00dfe"}, "p4")),
            result("p4", "OUTPUT:\n  %s %s\n  data:image/png;base64,%s\n  %s\nCHANGES:\n+ new Plate"
                   % (doc_file, person.replace(os.sep, "/"), pixels, pixels)),
            said("Saved in %s. Keys: %s" % (doc_file.upper() if not POSIX else doc_file, keys)),
        ]
        old = [asked("read the table"), said("", call("read_project_file", {"path": "refs/table.md"}, "o1")),
               result("o1", table), said("It is a thread table.")]
        info = training.begin("apikey", "build", agent, old + private[:1])
        rec = training.record(project, info, private, old + private)
        text = training.line(rec).decode("utf-8")
        msgs = rec["messages"]
        check("no saved key, no key of the request and no token of the bridge is in the line",
              not [k for k in keys.split() if k in text] and text.count("(a saved key, hidden)") >= 8
              and "(the token of the bridge, hidden)" in text, [k for k in keys.split() if k in text])
        check("the path of the project is PROJECT, in the message, the code, the result and the arguments",
              project not in text and os.path.realpath(project) not in text and "Bracket of Max" not in text
              and "The project is PROJECT and" in msgs[4]["content"]
              and json.loads(msgs[5]["tool_calls"][1]["function"]["arguments"]) == {"path": "PROJECT/refs"}
              and "PROJECT" in msgs[10]["content"] and "PROJECT" in msgs[9]["tool_calls"][0]["function"]["arguments"],
              text[:400])
        check("the home folder is ~ and the name of the user is not in the line",
              person not in text and "Max Mustermann" not in text and "Mustermann" not in text
              and "my folder is ~" in msgs[4]["content"], re.findall(r".{30}Mustermann.{10}", text))
        check("the text that read_project_file returned is a marker with the path and the number of characters",
              msgs[6]["content"] == training.FILE_TEXT % ("refs/table.md", len(table))
              and msgs[7]["content"] == training.FILE_TEXT % ("PROJECT/refs", len(private[3]["content"]))
              and "Thread table" not in text and "letter of" not in text, str(msgs[6:8]))
        check("... also in a context message", msgs[2] == {"role": "tool", "tool_call_id": "o1", "context": True,
                                                           "content": training.FILE_TEXT % ("refs/table.md", len(table))},
              str(msgs[2]))
        check("... an error of read_project_file is the tool's own text and stays",
              msgs[8]["content"] == "ERROR: there is no file refs/none.md in the project.", str(msgs[8]))
        check("a long line of a project file that the model repeats in its answer is taken out",
              "core hole 6.8" not in text and msgs[9]["content"] == "The book says: " + training.QUOTED, msgs[9]["content"])
        check("an image is its path in the project and a marker: no pixels",
              msgs[4]["images"] == ["refs/20261121-sketch.png"] and msgs[4]["image_data"] == training.NO_PIXELS
              and pixels not in text and "QUJDQUJD" not in text and training.IMAGE_DATA in msgs[10]["content"]
              and training.ENCODED % len(pixels) in msgs[10]["content"], msgs[10]["content"])
        args = json.loads(msgs[9]["tool_calls"][0]["function"]["arguments"])
        check("the arguments of a call are still JSON, and what needs no change is not changed",
              args["note"] == "Stra\u00dfe" and args["code"].startswith("print('PROJECT")
              and rec["messages"][1]["tool_calls"] == old[1]["tool_calls"], str(args))

        # other spellings of the same folders
        training._homes = lambda: {win_home}
        cleaner = training._Cleaner(win_project)
        for name, spelled, want in (
                ("Windows, backslashes", win_project + r"\model\P.FCStd", r"PROJECT\model\P.FCStd"),
                ("Windows, forward slashes as FreeCAD writes them", "C:/Users/Max Mustermann/CAD/Bracket/model/P.FCStd",
                 "PROJECT/model/P.FCStd"),
                ("Windows, another case", r"c:\users\max mustermann\cad\BRACKET\refs", r"PROJECT\refs"),
                ("doubled backslashes, as in JSON text and in repr()", r"C:\\Users\\Max Mustermann\\CAD\\Bracket\\x",
                 r"PROJECT\\x"),
                ("the home folder", r"C:\Users\Max Mustermann\Desktop\a.png", r"~\Desktop\a.png"),
                ("the short name that Windows gives a long folder", r"C:\Users\MAXMUS~1\AppData\Local\Temp\x.png",
                 r"~\AppData\Local\Temp\x.png"),
                ("the home folder of another user", "/home/teacher/books/table.md and /Users/anna/x",
                 "~/books/table.md and ~/x"),
                ("a folder that only begins like the project", r"C:\Users\Max Mustermann\CAD\Bracket2\x", r"~\CAD\Bracket2\x"),
                ("a path in the project that has a folder users", "PROJECT/users/list.md", "PROJECT/users/list.md"),
                ("a sentence that ends with the folder", "It is in C:/Users/Max Mustermann/CAD/Bracket.", "It is in PROJECT.")):
            check("paths: %s" % name, cleaner.text(spelled) == want, cleaner.text(spelled))
        training._homes = lambda: {"/data/people/max"}
        cleaner = training._Cleaner("/data/cad/p1")
        check("paths: /data/people/max is not found inside /data/people/maximilian, and max( ) in code stays",
              cleaner.text("/data/people/maximilian/x /data/people/max/x max(1, 2) /data/cad/p1/model /data/cad/p10")
              == "/data/people/maximilian/x ~/x max(1, 2) PROJECT/model /data/cad/p10",
              cleaner.text("/data/people/maximilian/x /data/people/max/x max(1, 2) /data/cad/p1/model /data/cad/p10"))
        check("paths: the top of a disk is no folder to look for",
              training._spellings("/") == set() and training._spellings("") == set())
    finally:
        training._homes = real_homes
        if real_bridge is None:
            del sys.modules["cadprompt.bridge"]
        else:
            sys.modules["cadprompt.bridge"] = real_bridge
        for forget in (backends.set_api_key, backends.set_local_key, backends.set_endpoint_token):
            forget("")
    check("the real home folder of this computer is in the list of folders to hide",
          os.path.expanduser("~") in training._homes() or os.path.dirname(os.path.expanduser("~")) == os.path.expanduser("~"),
          str(training._homes()))

    # ---- one line -------------------------------------------------------------------
    data = training.line({"a": "Ø50 × 5 – Stra\u00dfe\nnext\r\nline\u2028sep\u2029par\x85nel", "b": "half \udc80 character"})
    check("line(): one line of UTF-8 with LF at its end, and no other line end in it",
          data.endswith(b"\n") and data.count(b"\n") == 1 and b"\r" not in data
          and len(data.decode("utf-8").splitlines()) == 1 and "Ø50 × 5 – Straße".encode("utf-8") in data, repr(data))
    check("line(): it reads back as the same JSON object",
          json.loads(data) == {"a": "Ø50 × 5 – Stra\u00dfe\nnext\r\nline\u2028sep\u2029par\x85nel", "b": "half \udc80 character"})

    # ---- the file --------------------------------------------------------------------
    folder = os.path.join(project, "training")
    path = os.path.join(folder, "requests.jsonl")
    check("(setup) nothing made the folder so far", not os.path.lexists(folder))
    training.append(project, training.line({"n": 1}))
    training.append(project, training.line({"n": 2}))
    check("append(): makes training/ and requests.jsonl in the project, one line for each call",
          training.path_of(project) == path and read(path) == b'{"n":1}\n{"n":2}\n', repr(read(path)))
    if POSIX:
        check("append(): the folder and the file are for the current user only (0700, 0600)",
              stat.S_IMODE(os.stat(folder).st_mode) == 0o700 and stat.S_IMODE(os.stat(path).st_mode) == 0o600,
              "%o %o" % (os.stat(folder).st_mode, os.stat(path).st_mode))
        os.chmod(path, 0o644)
        training.append(project, training.line({"n": 3}))
        check("append(): a file that others could read is closed for them",
              stat.S_IMODE(os.stat(path).st_mode) == 0o600 and read(path).endswith(b'{"n":3}\n'))
    else:
        print("skip  file modes: this system has none", flush=True)
    with open(path, "wb") as f:
        f.write(b'{"n":1}\n{"half":')
    training.append(project, training.line({"n": 4}))
    check("append(): a last line without its end is not joined with the new one",
          read(path) == b'{"n":1}\n{"half":\n{"n":4}\n', repr(read(path)))

    outside = os.path.join(home, "outside")
    os.makedirs(os.path.join(outside, "folder"))
    target = os.path.join(outside, "notes.txt")
    with open(target, "wb") as f:
        f.write(b"the user's own file outside the project\n")

    def link(target, name, folder=False):
        """A link, or False where this system or this user cannot make one (Windows)."""
        try:
            os.symlink(target, name, target_is_directory=folder)
            return True
        except (OSError, NotImplementedError, AttributeError):
            return False

    def refused(where):
        try:
            training.append(where, training.line({"n": 9}))
        except OSError as exc:
            return str(exc) or exc.__class__.__name__
        return ""

    def new_project(name):
        made = os.path.join(home, "projects", name)
        os.makedirs(made)
        return made

    one = new_project("link-folder")
    if not link(os.path.join(outside, "folder"), os.path.join(one, "training"), folder=True):
        print("skip  links: this system or this user cannot make a symbolic link", flush=True)
    else:
        check("training/ is a link to a folder outside the project: refused, nothing is made there",
              refused(one) == training.A_LINK and os.listdir(os.path.join(outside, "folder")) == [], refused(one))
        two = new_project("link-inside")
        os.makedirs(os.path.join(two, "model"))
        link(os.path.join(two, "model"), os.path.join(two, "training"), folder=True)
        check("training/ is a link to another folder of the project: refused too",
              refused(two) == training.A_LINK and os.listdir(os.path.join(two, "model")) == [])
        three = new_project("link-file")
        os.makedirs(os.path.join(three, "training"))
        link(target, os.path.join(three, "training", "requests.jsonl"))
        check("requests.jsonl is a link to a file outside the project: refused, that file is not changed",
              bool(refused(three)) and read(target) == b"the user's own file outside the project\n", refused(three))
        four = new_project("link-nowhere")
        os.makedirs(os.path.join(four, "training"))
        link(os.path.join(outside, "not-there-yet"), os.path.join(four, "training", "requests.jsonl"))
        check("requests.jsonl is a link to a file that is not there: refused, no file is made outside",
              bool(refused(four)) and sorted(os.listdir(outside)) == ["folder", "notes.txt"], str(os.listdir(outside)))
        five = new_project("linked-project")
        os.rmdir(five)
        os.makedirs(os.path.join(outside, "real-place"))
        link(os.path.join(outside, "real-place"), five, folder=True)
        check("a project folder that is a link itself is the user's own choice: the file is written in it",
              refused(five) == "" and read(os.path.join(outside, "real-place", "training", "requests.jsonl")) == b'{"n":9}\n')
    six = new_project("hard-link")
    os.makedirs(os.path.join(six, "training"))
    try:
        os.link(target, os.path.join(six, "training", "requests.jsonl"))
    except (OSError, NotImplementedError, AttributeError):
        print("skip  hard links: this system or this disk cannot make one", flush=True)
    else:
        check("requests.jsonl is a second name of a file outside the project (a hard link): refused",
              refused(six) == training.A_LINK and read(target) == b"the user's own file outside the project\n", refused(six))
    seven = new_project("file-in-the-way")
    with open(os.path.join(seven, "training"), "wb") as f:
        f.write(b"a file with the name of the folder")
    check("training is a file, not a folder: an error, and the file is not changed",
          bool(refused(seven)) and read(os.path.join(seven, "training")) == b"a file with the name of the folder")
    eight = new_project("folder-in-the-way")
    os.makedirs(os.path.join(eight, "training", "requests.jsonl"))
    check("requests.jsonl is a folder: an error", bool(refused(eight)))
    check("a project folder that is not there: an error, and it is not made",
          bool(refused(os.path.join(home, "projects", "none"))) and not os.path.exists(os.path.join(home, "projects", "none")))

    # ---- keep(): the one call of the engine ---------------------------------------------
    info = training.begin("local", "build", agent, plate[:1])
    a, b = new_project("keep-a"), new_project("keep-b")
    first = training.keep(a, info, plate, 0, good, True)
    lines = (read(training.path_of(a)) or b"").splitlines()
    check("keep(): a good request is one line of valid JSON, and the chat gets the line that names the file",
          first == training.SAVED and len(lines) == 1 and json.loads(lines[0])["messages"][-1] == plate[-1], first)
    check("keep(): the next good request of the project is written and the chat gets nothing more",
          training.keep(a, info, plate, 0, good, True) == "" and len(read(training.path_of(a)).splitlines()) == 2)
    check("keep(): the first one of another project names the file again",
          training.keep(b, info, plate, 0, good, True) == training.SAVED)
    check("keep(): a request that did not end well writes nothing and says nothing",
          training.keep(a, info, plate, 0, dict(good, incomplete=True), True) == ""
          and training.keep(a, info, plate, 0, good, False) == ""
          and training.keep(a, info, skipped, 0, good, True) == ""
          and len(read(training.path_of(a)).splitlines()) == 2)
    with_context = training.keep(a, training.begin("local", "build", agent, earlier + plate[:1]),
                                 earlier + plate, len(earlier), good, True)
    last = json.loads(read(training.path_of(a)).splitlines()[-1])
    check("keep(): a follow-up request: the earlier messages are the context, the request begins at its own message",
          with_context == "" and [bool(m.get("context")) for m in last["messages"]] == [True] * 4 + [False] * 4
          and last["messages"][4]["content"] == "make a plate", str(last["messages"])[:300])
    said_once = training.keep(seven, info, plate, 0, good, True)
    check("keep(): a file that cannot be written: no exception, one plain line for the chat",
          said_once.startswith("The training data could not be written to `training/requests.jsonl`")
          and said_once.endswith("Your request is not affected.") and "Traceback" not in said_once, said_once)
    check("keep(): ... and only once in a session",
          training.keep(seven, info, plate, 0, good, True) == "" and training.keep(eight, info, plate, 0, good, True) == "")
    check("keep(): broken input raises nothing",
          training.keep(a, None, None, 0, None, True) == "" and training.keep(None, {}, [1, 2], 5, {}, True) == "")
    check("the file of the first project has its three lines, each one a JSON object with the version 1",
          [json.loads(row)["version"] for row in read(training.path_of(a)).splitlines()] == [1, 1, 1])


try:
    main()
except Exception:
    traceback.print_exc()
    FAILED.append("the test script itself")
print(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
      + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"), flush=True)
os._exit(1 if FAILED else 0)
