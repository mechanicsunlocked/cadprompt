"""Runs every test of the addon with one command, on Linux, macOS and Windows.

    python3 tests/run_all.py                 all tests
    python3 tests/run_all.py -k panel        only tests with "panel" in their name
    python3 tests/run_all.py --list          show the tests, run nothing
    python3 tests/run_all.py --no-gui        skip the tests that need the FreeCAD window

A test file says how it must be started with one comment line in its first
30 lines:

    # RUN: python          plain Python, no FreeCAD
    # RUN: freecadcmd      FreeCAD without a window
    # RUN: freecad-gui     FreeCAD with its window (no display needed)
    # RUN: manual          needs a real model or the real claude: never run here

Every test gets a new, empty FreeCAD settings folder (FREECAD_USER_HOME), so
the user's own FreeCAD settings are never read or changed. tests/README.md
has the details. Standard library only.
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

KINDS = ("python", "freecadcmd", "freecad-gui", "manual")
MARKER = re.compile(r"^\s*#\s*RUN:\s*(\S+)")
MARKER_LINES = 30

# The test files that existed before the '# RUN:' line was agreed. A marker in
# the file wins; an entry here can go when its file has the marker.
BUILT_IN = {
    "test_backends": "freecadcmd",
    "test_drawing_headless": "freecadcmd",
    "test_local_tools": "freecadcmd",
    "test_local_agent": "python",
    "hoischen/test_layout": "python",
    "test_drawing_gui": "freecad-gui",
    "test_integration_gui": "freecad-gui",
    "test_local_engine": "freecad-gui",
    "test_panel": "freecad-gui",
    "test_panel_engine": "freecad-gui",
    "hoischen/gui_check": "freecad-gui",
}

# What one file needs beyond its kind. {home} is the test's FREECAD_USER_HOME.
# gui_check.py is written for a FreeCAD that has the addon installed and its
# drawing buttons in TechDraw (the user's choice ShowInTechDraw, off in a new
# profile). It stays open unless HOISCHEN_EXIT is set, and it writes a JSON
# report, not a .log.
SPECIAL = {
    "hoischen/gui_check": {
        "args": ["--module-path", REPO],
        "env": {"HOISCHEN_EXIT": "1", "HOISCHEN_REPORT": os.path.join("{home}", "gui_check.json")},
        "settings": """          <FCParamGroup Name="CADprompt">
            <FCBool Name="ShowInTechDraw" Value="1"/>
          </FCParamGroup>
""",
        "log": "gui_check.json",
    },
}

# With the offscreen platform FreeCAD 1.1 locks up as soon as it shows a
# notification, and a new settings folder brings up the first-start dialog,
# which waits for a click. Both are switched off. Same text as in drawing_run.py.
USER_CFG = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FCParameters>
  <FCParamGroup Name="Root">
    <FCParamGroup Name="BaseApp">
      <FCParamGroup Name="Preferences">
        <FCParamGroup Name="NotificationArea">
          <FCBool Name="NotificationAreaEnabled" Value="0"/>
          <FCBool Name="NonIntrusiveNotificationsEnabled" Value="0"/>
        </FCParamGroup>
        <FCParamGroup Name="Mod">
          <FCParamGroup Name="Start">
            <FCBool Name="FirstStart2024" Value="0"/>
            <FCBool Name="ShowOnStartup" Value="0"/>
          </FCParamGroup>
        </FCParamGroup>
      </FCParamGroup>
    </FCParamGroup>
  </FCParamGroup>
</FCParameters>
"""

# FreeCAD does not stop when a test script raises or just ends. freecadcmd then
# leaves with exit code 0, which would look like a pass. So for freecadcmd this
# second script goes on the command line after the test: a test that gives its
# own result never reaches it, a test that broke before that ends here.
NO_RESULT = 97
END_SCRIPT = "import os\nos._exit(%d)\n" % NO_RESULT
# With the window, FreeCAD stays open after such a script and some tests go on
# from the event loop on purpose, so there the output is watched for this line.
LOAD_ERROR = "Exception while processing file"

# Writes which FreeCAD this is, and if it read our user.cfg, to a file:
# on Windows the FreeCAD window program prints nothing we could read.
PROBE_SCRIPT = """import os, sys
import FreeCAD as App
v = App.Version()
seen = not App.ParamGet("User parameter:BaseApp/Preferences/Mod/Start").GetBool("ShowOnStartup", True)
with open(os.path.join(os.environ["FREECAD_USER_HOME"], "version.txt"), "w", encoding="utf-8") as f:
    f.write("FreeCAD %s.%s.%s (%s), Python %s\\n" % (v[0], v[1], v[2], v[3], sys.version.split()[0]))
    f.write("cfg\\n" if seen else "nocfg\\n")
os._exit(0)
"""


def say(text=""):
    print(text, flush=True)


# ---------------------------------------------------------------- FreeCAD

def _program(folder, names):
    for name in names:
        for ending in ((".exe", "") if os.name == "nt" else ("",)):
            path = os.path.join(folder, name + ending)
            if os.path.isfile(path):
                return path
    return None


def _in_folder(folder):
    """(window program, console program) of the FreeCAD in an install folder or a macOS .app."""
    mac_gui, mac_cmd = os.path.join("Contents", "MacOS"), os.path.join("Contents", "Resources", "bin")
    gui = cmd = None
    # macOS: the program in MacOS sets up the paths and then starts Resources/bin/freecad
    for sub in (mac_gui, "bin", "", mac_cmd):
        gui = gui or _program(os.path.join(folder, sub), ("FreeCAD", "freecad"))
    for sub in (mac_cmd, "bin", "", mac_gui):
        cmd = cmd or _program(os.path.join(folder, sub), ("FreeCADCmd", "freecadcmd"))
    return gui, cmd


def _is_appimage(path):
    # 'AppRun' is the same start script in an unpacked AppImage (squashfs-root/AppRun)
    return path.lower().endswith(".appimage") or os.path.basename(path) == "AppRun"


def _usual_folders():
    found = []
    if sys.platform == "win32":
        for name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(name)
            if base:
                base = os.path.join(base, "Programs") if name == "LOCALAPPDATA" else base
                found += sorted(glob.glob(os.path.join(base, "FreeCAD*")), reverse=True)     # newest first
    elif sys.platform == "darwin":
        for base in ("/Applications", os.path.expanduser("~/Applications")):
            found.append(os.path.join(base, "FreeCAD.app"))
            found += sorted(glob.glob(os.path.join(base, "FreeCAD*.app")), reverse=True)
    return [f for f in found if os.path.isdir(f)]


def _near(path):
    """(window program, console program) that belong to one FREECAD or FREECADCMD value."""
    if _is_appimage(path):
        # the AppImage start script runs the program in its usr/bin that the first word names
        return [path, "freecad"], [path, "freecadcmd"]
    gui = cmd = None
    folders = [path]
    if not os.path.isdir(path):                          # bin/FreeCAD, or FreeCAD.app/Contents/MacOS/FreeCAD
        folder = os.path.dirname(os.path.abspath(path))
        folders = [folder, os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))]
    for folder in folders:
        found = _in_folder(folder)
        gui, cmd = gui or found[0], cmd or found[1]
    return ([gui] if gui else None), ([cmd] if cmd else None)


def find_freecad():
    """{'gui': start of the command line or None, 'cmd': the same, 'error': text or None}.

    FREECAD and FREECADCMD first, then PATH, then the usual install folders.
    FREECAD can also be an install folder, a macOS .app or an AppImage.
    """
    out = {"gui": None, "cmd": None, "error": None}
    given = {n: os.environ.get(n, "").strip() for n in ("FREECAD", "FREECADCMD")}
    for name, path in given.items():
        if path and not os.path.exists(path):
            out["error"] = "%s is set to %s, which does not exist." % (name, path)
            return out

    if given["FREECAD"] or given["FREECADCMD"]:
        # A named file is used as it is. The other program comes only from the
        # same install, never from a second FreeCAD somewhere in PATH.
        def exact(path):
            return [path] if path and os.path.isfile(path) and not _is_appimage(path) else None

        by_gui = _near(given["FREECAD"]) if given["FREECAD"] else (None, None)
        by_cmd = _near(given["FREECADCMD"]) if given["FREECADCMD"] else (None, None)
        out["gui"] = exact(given["FREECAD"]) or by_gui[0] or by_cmd[0]
        out["cmd"] = exact(given["FREECADCMD"]) or by_cmd[1] or by_gui[1]
        return out

    for key, names in (("gui", ("freecad", "FreeCAD")), ("cmd", ("freecadcmd", "FreeCADCmd"))):
        for name in names:
            path = shutil.which(name)
            if path:
                out[key] = [path]
                break
    if not out["gui"] and not out["cmd"]:
        for folder in _usual_folders():
            gui, cmd = _in_folder(folder)
            if gui or cmd:
                out["gui"], out["cmd"] = ([gui] if gui else None), ([cmd] if cmd else None)
                break
    return out


# ---------------------------------------------------------------- tests

def read_marker(path):
    """The kind in the file's '# RUN:' line, None without one, or the wrong word as ('?', word)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(MARKER_LINES):
                m = MARKER.match(f.readline())
                if m:
                    return m.group(1) if m.group(1) in KINDS else ("?", m.group(1))
    except OSError:
        pass
    return None


def guess_kind(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return "python"
    if re.search(r"^\s*(import|from)\s+(FreeCADGui|PySide)", text, re.M) or "FreeCADGui" in text:
        return "freecad-gui"
    if re.search(r"^\s*(import|from)\s+FreeCAD\b", text, re.M):
        return "freecadcmd"
    return "python"


def discover():
    """[{'name', 'path', 'kind', 'origin', 'problem'}]: every tests/**/test_*.py and the built-in list."""
    paths = set()
    for folder, subs, files in os.walk(HERE):
        subs[:] = [s for s in subs if s != "__pycache__" and not s.startswith(".")]
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                paths.add(os.path.join(folder, f))
    for name in BUILT_IN:
        path = os.path.join(HERE, *name.split("/")) + ".py"
        if os.path.isfile(path):
            paths.add(path)
    tests = []
    for path in sorted(paths):
        name = os.path.relpath(path, HERE)[:-3].replace(os.sep, "/")
        marker = read_marker(path)
        test = {"name": name, "path": path, "problem": None}
        if isinstance(marker, tuple):
            test.update(kind="manual", origin="marker",
                        problem="'# RUN: %s' is not one of: %s" % (marker[1], ", ".join(KINDS)))
        elif marker:
            test.update(kind=marker, origin="marker")
        elif name in BUILT_IN:
            test.update(kind=BUILT_IN[name], origin="built-in list")
        else:
            test.update(kind=guess_kind(path), origin="guessed: add a '# RUN:' line")
        tests.append(test)
    order = {k: i for i, k in enumerate(KINDS)}          # the fast ones first
    return sorted(tests, key=lambda t: (order[t["kind"]], t["name"]))


# ---------------------------------------------------------------- running

def base_env():
    env = dict(os.environ)
    for name in list(env):
        # *_REAL_* and CADPROMPT_SMOKE_* switch on a run with a real model: never from here.
        # No key of this computer goes into a test either.
        if "_REAL_" in name or name.startswith(("CADPROMPT_SMOKE_", "ANTHROPIC_")) or name == "OPENAI_API_KEY":
            del env[name]
    env["PYTHONDONTWRITEBYTECODE"] = "1"                 # no __pycache__ in the repository
    env["PYTHONUNBUFFERED"] = "1"                        # keep the output of a test that is killed
    return env


def fresh_home(folder, settings=""):
    """A new FreeCAD settings folder. settings: more groups for Preferences/Mod in its user.cfg."""
    home = os.path.join(folder, "home")
    os.makedirs(home)
    mod = '<FCParamGroup Name="Mod">\n'
    with open(os.path.join(home, "user.cfg"), "w", encoding="utf-8") as f:
        f.write(USER_CFG.replace(mod, mod + settings))
    return home


def offscreen(env):
    """No window on the screen, and no display needed."""
    env["QT_QPA_PLATFORM"] = "offscreen"
    # the start script of the FreeCAD AppImage (after 1.1) only listens to this name
    env["FREECAD_QT_QPA_PLATFORM"] = "offscreen"


def kill_tree(proc):
    if os.name == "nt":
        try:        # taskkill is a part of Windows; /T takes the child processes too
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:        # the test is the leader of its own process group, see run()
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def load_error(out_path):
    try:
        with open(out_path, encoding="utf-8", errors="replace") as f:
            return LOAD_ERROR in f.read()
    except OSError:
        return False


def run(cmd, env, out_path, seconds, watch=False):
    """(exit code, seconds it took). The code is None when the time ran out, and
    NO_RESULT when watch is on and FreeCAD could not load the script but stayed open."""
    more = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    start = time.monotonic()
    with open(out_path, "wb") as out:
        try:
            proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, **more)
        except OSError as exc:
            out.write(("could not start %s: %s\n" % (cmd[0], exc)).encode("utf-8"))
            return 127, 0.0
        seen = None                                      # when the load error was first seen
        try:
            while True:
                try:
                    code = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if now - start > seconds:
                        code = None
                        break
                    if watch and seen is None and load_error(out_path):
                        seen = now
                    if seen is not None and now - seen > 5:      # it had its chance to end by itself
                        code = NO_RESULT
                        break
        except KeyboardInterrupt:
            kill_tree(proc)
            raise
        if proc.poll() is None:
            kill_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
    return code, time.monotonic() - start


def last_lines(path, count=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [line.rstrip() for line in f.read().splitlines() if line.strip()]
    except OSError:
        return []
    short = []
    for line in lines:                                   # FreeCAD repeats some lines many times
        if short and short[-1][0] == line:
            short[-1][1] += 1
        else:
            short.append([line, 1])
    return [line + ("   (%d times)" % n if n > 1 else "") for line, n in short[-count:]]


def json_report(path):
    """The failed checks and the errors of a report like the one gui_check.py writes."""
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
        lines = ["FAIL  %s   [%s]" % (c.get("name"), c.get("detail", "")) for c in report.get("checks", [])
                 if not c.get("ok")]
        for error in report.get("errors", []):
            lines += str(error).rstrip().splitlines()[-12:]
        return lines or last_lines(path)
    except (OSError, ValueError, AttributeError):
        return last_lines(path)


def probe(freecad, root, env):
    """One line that says which FreeCAD this is, and a warning or None."""
    start = freecad["cmd"] or freecad["gui"]
    if not start:
        return "FreeCAD: not found", None
    folder = os.path.join(root, "_freecad-version")
    os.makedirs(folder)
    home = fresh_home(folder)
    script = os.path.join(folder, "run_all_version.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(PROBE_SCRIPT)
    env = dict(env, FREECAD_USER_HOME=home)
    offscreen(env)
    out = os.path.join(folder, "output.txt")
    code, _ = run(start + [script], env, out, 180)
    lines = last_lines(os.path.join(home, "version.txt"))
    if code == 0 and lines:
        warning = None
        if lines[-1] == "nocfg":
            warning = ("this FreeCAD did not read the user.cfg in FREECAD_USER_HOME: the first-start dialog "
                       "can stop the tests that need the window")
        return lines[0], warning
    # the script did not run: ask the program itself (still with a throw-away settings folder)
    code, _ = run(start + ["--version"], env, out, 60)
    lines = [line for line in last_lines(out) if "FreeCAD" in line]
    if code == 0 and lines:
        return lines[0], "FreeCAD started, but it did not run a script"
    return "FreeCAD: version not known", "FreeCAD did not start:\n" + "\n".join(last_lines(out, 10))


def appimage_ignores_offscreen(freecad, root, env):
    """True when the AppImage start script puts its own QT_QPA_PLATFORM in place of ours (FreeCAD 1.1 does)."""
    start = freecad["gui"]
    if not start or not _is_appimage(start[0]):
        return False
    env = dict(env, FREECAD_USER_HOME=os.path.join(root, "_freecad-version", "home"))
    offscreen(env)
    out = os.path.join(root, "_freecad-version", "platform.txt")
    code, _ = run([start[0], "python", "-c", "import os; print('QPA=' + os.environ.get('QT_QPA_PLATFORM', ''))"],
                  env, out, 60)
    lines = [line for line in last_lines(out) if line.startswith("QPA=")]
    return code == 0 and bool(lines) and lines[-1] != "QPA=offscreen"


def run_test(test, freecad, root, env, args):
    """{'status': PASS, FAIL, TIMEOUT or SKIP, 'seconds', 'why', 'lines', 'folder'}."""
    result = {"status": "SKIP", "seconds": 0.0, "why": "", "lines": [], "folder": None}
    kind = test["kind"]
    if test["problem"]:
        return dict(result, status="FAIL", why=test["problem"])
    if kind == "manual":
        return dict(result, why="manual: needs a real model or the real claude program")
    if kind == "freecad-gui" and args.no_gui:
        return dict(result, why="--no-gui")
    if kind != "freecad-gui" and args.gui_only:
        return dict(result, why="--gui-only")
    if kind == "python":
        start = [sys.executable]
    else:
        start = freecad["cmd" if kind == "freecadcmd" else "gui"]
        if not start:
            what = "freecadcmd (set FREECADCMD)" if kind == "freecadcmd" else "freecad (set FREECAD)"
            return dict(result, status="FAIL", why=freecad["error"] or "not found: " + what)

    folder = os.path.join(root, test["name"].replace("/", "-"))
    os.makedirs(folder)
    special = SPECIAL.get(test["name"], {})
    home = fresh_home(folder, special.get("settings", ""))
    env = dict(env, FREECAD_USER_HOME=home)
    for name, value in special.get("env", {}).items():
        env[name] = value.replace("{home}", home)
    cmd = start + ([] if kind == "python" else list(special.get("args", []))) + [test["path"]]
    if kind == "freecadcmd":
        end = os.path.join(folder, "run_all_end.py")
        with open(end, "w", encoding="utf-8") as f:
            f.write(END_SCRIPT)
        cmd.append(end)

    for name, key in (("FREECAD", "gui"), ("FREECADCMD", "cmd")):
        # for a test that starts FreeCAD itself: the same FreeCAD, when it is one program file
        if freecad[key] and len(freecad[key]) == 1:
            env[name] = freecad[key][0]

    out = os.path.join(folder, "output.txt")
    code, seconds = run(cmd, env, out, args.timeout, watch=kind == "freecad-gui")
    result.update(seconds=seconds, folder=folder)
    if code == 0:
        return dict(result, status="PASS")

    stem = os.path.basename(test["path"])[:-3]
    logs = [os.path.join(home, n) for n in (special.get("log"), stem + ".log", stem + ".txt") if n]
    log = next((p for p in logs if os.path.isfile(p) and os.path.getsize(p)), None)
    lines = []
    if log and log.endswith(".json"):
        lines += ["-- " + os.path.basename(log)] + json_report(log)
    elif log:
        tail = last_lines(log)
        failed = [line for line in last_lines(log, 100000) if line.startswith(("FAIL", "ERROR")) and line not in tail]
        lines += ["-- " + os.path.basename(log)] + failed[:10] + (["..."] if failed else []) + tail
    if not log or code in (None, NO_RESULT):
        lines += ["-- output"] + (last_lines(out) or ["(nothing)"])
    if code is None:
        return dict(result, status="TIMEOUT", why="stopped after %d s" % args.timeout, lines=lines)
    why = "exit code %d" % code
    if code == NO_RESULT:
        why = "the script gave no result: an error while FreeCAD loaded it, or no os._exit at its end"
    return dict(result, status="FAIL", why=why, lines=lines)


def count(results, status):
    return sum(1 for _, r in results if r["status"] == status)


def write_junit(path, results, total, version):
    def clean(text):                                     # XML 1.0 does not allow most control characters
        return "".join(c if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD else "?"
                       for c in text)

    suite = ET.Element("testsuite", name="cadprompt", tests=str(len(results)),
                       failures=str(count(results, "FAIL")), errors=str(count(results, "TIMEOUT")),
                       skipped=str(count(results, "SKIP")), time="%.1f" % total)
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", name="freecad", value=clean(version))
    for test, r in results:
        case = ET.SubElement(suite, "testcase", name=test["name"], classname=test["kind"], time="%.1f" % r["seconds"])
        tag = {"FAIL": "failure", "TIMEOUT": "error", "SKIP": "skipped"}.get(r["status"])
        if tag:
            ET.SubElement(case, tag, message=clean(r["why"])).text = clean("\n".join(r["lines"]))
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main():
    ap = argparse.ArgumentParser(description="Run the CADprompt tests. See tests/README.md.")
    ap.add_argument("-k", action="append", default=[], metavar="TEXT",
                    help="only tests with TEXT in their name (can be given more than once)")
    ap.add_argument("--list", action="store_true", help="show the tests and how each one is started, run nothing")
    ap.add_argument("--no-gui", action="store_true", help="skip the tests that need the FreeCAD window")
    ap.add_argument("--gui-only", action="store_true", help="only the tests that need the FreeCAD window")
    ap.add_argument("--display", action="store_true", help="show the FreeCAD window on the screen (default: no display)")
    ap.add_argument("--timeout", type=int, default=300, metavar="S", help="seconds for one test (default 300)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folders of all tests")
    ap.add_argument("--out", metavar="FOLDER", help="put the test folders into FOLDER and keep them")
    ap.add_argument("--junit", metavar="FILE", help="also write the results as JUnit XML")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):              # a Windows console cannot print every character
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    tests = [t for t in discover() if not args.k or any(k in t["name"] for k in args.k)]
    if not tests:
        say("No test matches.")
        return 1
    width = max(len(t["name"]) for t in tests)
    if args.list:
        for t in tests:
            say("%-11s  %-*s  (%s)" % (t["kind"], width, t["name"], t["problem"] or t["origin"]))
        return 0

    if args.out:
        root = first = os.path.join(os.path.abspath(args.out), time.strftime("run-%Y%m%d-%H%M%S"))
        n = 1
        while os.path.exists(root):                      # two runs in the same second
            n += 1
            root = "%s-%d" % (first, n)
        os.makedirs(root)
    else:
        root = tempfile.mkdtemp(prefix="cadprompt-tests-")
    env = base_env()
    if not args.display:
        offscreen(env)           # for every test: a plain Python test can start the FreeCAD window too
    freecad = find_freecad()
    wanted = {t["kind"] for t in tests} - ({"freecad-gui"} if args.no_gui else set())
    if args.gui_only:
        wanted &= {"freecad-gui"}
    version = "FreeCAD: not needed"
    if wanted & {"freecadcmd", "freecad-gui"}:
        version, warning = probe(freecad, root, env)
        say(version)
        for key, label in (("cmd", "freecadcmd"), ("gui", "freecad")):
            say("  %-10s  %s" % (label, " ".join(freecad[key]) if freecad[key] else "not found"))
        if freecad["error"]:
            say("  " + freecad["error"])
        if warning:
            say("  WARNING: " + warning)
        if "freecad-gui" in wanted and not args.display and appimage_ignores_offscreen(freecad, root, env):
            say("  NOTE: this AppImage always uses the X display (xcb). The window tests need a display, "
                "for example xvfb-run. tests/README.md says how to run them without one.")
        say()

    results = []
    began = time.monotonic()
    try:
        for t in tests:
            r = run_test(t, freecad, root, env, args)
            results.append((t, r))
            took = "%6.1f s" % r["seconds"] if r["folder"] else " " * 8
            say("%-7s %s  %-11s  %-*s%s" % (r["status"], took, t["kind"], width, t["name"],
                                            "  (%s)" % r["why"] if r["why"] else ""))
            for line in r["lines"]:
                say("        | " + line)
    except KeyboardInterrupt:
        say("\nStopped.")
    total = time.monotonic() - began

    bad = [t["name"] for t, r in results if r["status"] in ("FAIL", "TIMEOUT")]
    stopped = len(results) < len(tests)
    say()
    say("%d tests in %.0f s: %d passed, %d failed, %d timed out, %d skipped"
        % (len(results), total, count(results, "PASS"), count(results, "FAIL"), count(results, "TIMEOUT"),
           count(results, "SKIP")))
    if bad:
        say("Not passed: " + ", ".join(bad))
    unmarked = [t["name"] for t in tests if t["origin"].startswith("guessed")]
    if unmarked:
        say("No '# RUN:' line, so the way to start it was guessed: " + ", ".join(unmarked))
    if args.junit:
        write_junit(args.junit, results, total, version)

    if not (args.keep or args.out):
        for t, r in results:                             # keep only what helps to find a failure
            if r["folder"] and r["status"] == "PASS":
                shutil.rmtree(r["folder"], ignore_errors=True)
        shutil.rmtree(os.path.join(root, "_freecad-version"), ignore_errors=True)
        if not os.listdir(root):
            shutil.rmtree(root, ignore_errors=True)
    if os.path.isdir(root):
        say(("The test folders are in " if args.keep or args.out else "The folders of the failed tests are in ") + root)
    return 1 if bad or stopped else 0


if __name__ == "__main__":
    sys.exit(main())
