# SPDX-License-Identifier: MIT
"""Tests of the CADprompt workbench, with the addon loaded by FreeCAD itself.

    python3 tests/test_workbench.py [folder for the results]

Without a folder the results go to a temporary one, which is removed again
when every check passed.

Every run gets a fresh FREECAD_USER_HOME whose Mod folder holds a link to this
repository, so FreeCAD finds and starts the addon the way it does after an
Addon Manager install. The user's own FreeCAD settings are never read or changed.

1. package.xml and InitGui.py, read as files,
2. freecadcmd: Init.py does nothing and gives no error            (workbench_cmd_check.py)
3. freecad without a display, a new profile                        (workbench_gui_check.py)
4. the same with ShowInTechDraw=True in the profile before the start,
5. the same with FreeCAD's TechDraw workbench switched off,
6. the same with CADprompt as the workbench that FreeCAD starts in,
7. the time the addon adds to FreeCAD's start                      (workbench_startup_time.py)

FREECAD and FREECADCMD name the programs when they are not in PATH.
No model, no server and no `claude` are called: the profile chooses the local
model without a server address, so the chat panel only shows its setup message.
"""
# RUN: python

import ast
import os
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)

from drawing_run import run                                 # noqa: E402

NS = "{https://wiki.freecad.org/Package_Metadata}"
FREECAD = os.environ.get("FREECAD") or "freecad"
FREECADCMD = os.environ.get("FREECADCMD") or "freecadcmd"
STARTUP_LIMIT_MS = 100       # the measured value is far below; this only catches a heavy import
FAILED = []
COUNT = [0]

# The first two groups keep FreeCAD from locking up without a display (see
# drawing_run.py). The CADprompt group keeps the chat panel quiet: local model,
# no server address, projects inside the profile.
USER_CFG = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FCParameters>
  <FCParamGroup Name="Root">
    <FCParamGroup Name="BaseApp">
      <FCParamGroup Name="Preferences">{general}
        <FCParamGroup Name="NotificationArea">
          <FCBool Name="NotificationAreaEnabled" Value="0"/>
          <FCBool Name="NonIntrusiveNotificationsEnabled" Value="0"/>
        </FCParamGroup>
        <FCParamGroup Name="Mod">
          <FCParamGroup Name="Start">
            <FCBool Name="FirstStart2024" Value="0"/>
            <FCBool Name="ShowOnStartup" Value="0"/>
          </FCParamGroup>
          <FCParamGroup Name="CADprompt">
            <FCText Name="Backend">local</FCText>
            <FCText Name="LocalUrl"></FCText>
            <FCText Name="ProjectsDir">{projects}</FCText>{more}
          </FCParamGroup>
        </FCParamGroup>
      </FCParamGroup>
    </FCParamGroup>
  </FCParamGroup>
</FCParameters>
"""
OPT_IN = '\n            <FCBool Name="ShowInTechDraw" Value="1"/>'
START_IN_OURS = """
        <FCParamGroup Name="General">
          <FCText Name="AutoloadModule">CADpromptWorkbench</FCText>
        </FCParamGroup>"""


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    print(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def fresh_home(root, name, addon=True, opt_in=False, start_in_ours=False):
    """A new FreeCAD profile. With FREECAD_USER_HOME set, FreeCAD keeps user.cfg and Mod/ right in it."""
    home = os.path.join(root, name)
    os.makedirs(os.path.join(home, "Mod"))
    with open(os.path.join(home, "user.cfg"), "w", encoding="utf-8") as f:
        f.write(USER_CFG.format(projects=os.path.join(home, "projects"), more=OPT_IN if opt_in else "",
                                general=START_IN_OURS if start_in_ours else ""))
    if addon:
        link = os.path.join(home, "Mod", "CADprompt")
        try:
            os.symlink(REPO, link, target_is_directory=True)
        except OSError:      # Windows without the right to make links: a copy is loaded the same way
            shutil.copytree(REPO, link, ignore=shutil.ignore_patterns(".git", "__pycache__", "vendor"))
    return home


def environment(home, **more):
    return dict(os.environ, FREECAD_USER_HOME=home, PYTHONDONTWRITEBYTECODE="1", **more)


def show(path, out):
    """Print a report that a script inside FreeCAD wrote; the check lines count here too."""
    if not os.path.isfile(path):
        print("no report was written; FreeCAD said:\n" + out[-3000:])
        return False
    with open(path, encoding="utf-8") as f:
        text = f.read().rstrip()
    print(text)
    COUNT[0] += len(re.findall(r"^(?:ok   |FAIL ) ", text, re.M))
    FAILED.extend(re.findall(r"^FAIL  (.*?)(?:   \[.*)?$", text, re.M))
    return True


def records(path):
    """FreeCAD's log as (kind, text) pairs; lines without a 'Log: ' style start belong to the one before."""
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            m = re.match(r"(Log|Msg|Wrn|Err|Critical|Ntf): ?(.*)", line.rstrip("\n"))
            if m:
                out.append([m.group(1), m.group(2)])
            elif out:
                out[-1][1] += "\n" + line.rstrip("\n")
    return out


def check_log(what, path, home, inits):
    """No error from our addon in FreeCAD's log of the whole run, and its init files ran."""
    recs = records(path)
    mod = os.path.join(home, "Mod", "CADprompt")
    done = [t for k, t in recs if "Initializing" in t and mod in t and t.rstrip().endswith("done")]
    check(f"{what}: FreeCAD's log says it ran the addon's init file" + ("s (Init.py, InitGui.py)" if inits == 2 else " (Init.py)"),
          len(done) == inits, f"{len(done)} of {inits} in {path}")

    def ours(text):
        # the folder names of this run must not count as a mention of the addon
        rest = text.replace(mod, "<ADDON>").replace(home, "").replace(REPO, "").lower()
        return "<addon>" in rest or "cadprompt" in rest or "hoischen" in rest

    def bad(kind, text):
        return (kind in ("Err", "Critical") or "Traceback" in text or "failed" in text
                or "Failed to get handle" in text or "check classname" in text)

    errors = [f"{k}: {t}" for k, t in recs if bad(k, t) and ours(t)]
    check(f"{what}: no error line from the addon in FreeCAD's log", bool(recs) and not errors,
          "\n".join(errors)[:2000] or f"empty log {path}")
    others = [f"{k}: {t}" for k, t in recs if k in ("Err", "Critical") and not ours(t)]
    if others:
        print(f"      ({len(others)} error lines in the log are not about the addon, first: {others[0][:160]})")


def static_checks():
    print("== 1. package.xml and InitGui.py")
    pkg = ET.parse(os.path.join(REPO, "package.xml")).getroot()

    def texts(path):
        return [(e.text or "").strip() for e in pkg.findall(path.replace("/", "/" + NS).replace("^", NS))]

    content = pkg.find(NS + "content")
    benches = content.findall(NS + "workbench") if content is not None else []
    check("package.xml: one content item, a workbench in the repository root",
          content is not None and len(list(content)) == 1 and len(benches) == 1
          and texts("^content/workbench/subdirectory") == ["./"], str(texts("^content/workbench/subdirectory")))
    classname = (texts("^content/workbench/classname") or [""])[0]
    with open(os.path.join(REPO, "InitGui.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)
               and any(ast.unparse(b) in ("Gui.Workbench", "Workbench") for b in n.bases)]
    added = [ast.unparse(n.args[0].func) for n in ast.walk(tree)
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "Gui.addWorkbench" and n.args
             and isinstance(n.args[0], ast.Call)]
    check("package.xml: <classname> is the one workbench class that InitGui.py defines and adds",
          classname and classes == [classname] and added == [classname], f"{classname!r} {classes} {added}")
    check("package.xml: version 0.2.0, FreeCAD 1.0.0 or newer, no upper limit",
          texts("^version") == ["0.2.0"] and texts("^freecadmin") == ["1.0.0"] and texts("^freecadmax") == []
          and not pkg.findall(".//" + NS + "freecadmax"), f"{texts('^version')} {texts('^freecadmin')}")
    licences = {(e.text or "").strip(): e.get("file") for e in pkg.findall(NS + "license")}
    check("package.xml: both licences, each with its file",
          licences == {"MIT": "LICENSE", "LGPL-2.1-or-later": "hoischen/LICENSE"}
          and all(os.path.isfile(os.path.join(REPO, *f.split("/"))) for f in licences.values()), str(licences))
    repo = [e for e in pkg.findall(NS + "url") if e.get("type") == "repository"]
    check("package.xml: the repository url names the branch main",
          len(repo) == 1 and repo[0].get("branch") == "main" and (repo[0].text or "").startswith("https://"))
    icons = texts("^icon") + texts("^content/workbench/icon")
    check("package.xml: the icons it names exist",
          len(icons) == 2 and all(os.path.isfile(os.path.join(REPO, *i.split("/"))) for i in icons), str(icons))
    words = " ".join(texts("^name") + texts("^description") + texts("^content/workbench/name")).lower()
    vendors = [v for v in ("claude", "anthropic", "openai", "chatgpt", "gpt", "gemini", "google", "gemma",
                           "copilot", "microsoft", "mistral", "ollama", "lm studio", "llama", "qwen")
               if re.search(r"\b" + re.escape(v) + r"\b", words)]
    check("package.xml: name and description name no AI vendor or product", not vendors, str(vendors))
    with open(os.path.join(REPO, "Init.py"), encoding="utf-8") as f:
        init = ast.parse(f.read())
    check("Init.py holds no code", init.body == [])
    print()


def main():
    temporary = None if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="cadprompt-workbench-")
    root = os.path.join(os.path.abspath(temporary or sys.argv[1]), time.strftime("run-%Y%m%d-%H%M%S"))
    os.makedirs(root)
    print(f"results in {root}\n")

    static_checks()

    print("== 2. freecadcmd")
    home = fresh_home(root, "home-cmd")
    log = os.path.join(home, "FreeCAD.log")
    code, out = run([FREECADCMD, "--log-file", log, os.path.join(HERE, "workbench_cmd_check.py")], environment(home))
    shown = show(os.path.join(home, "workbench_cmd_check.log"), out)
    check("freecadcmd: the run ended well", shown and code == 0, f"exit code {code}")
    check_log("freecadcmd", log, home, 1)
    print()

    cases = (("default", "3. freecad, a new profile", [], False),
             ("optin", "4. freecad, ShowInTechDraw=True before the start", [], True),
             ("notechdraw", "5. freecad without the TechDraw workbench (and ShowInTechDraw=True)",
              ["--disable-addon", "TechDraw"], True),
             ("autoload", "6. freecad that starts in the CADprompt workbench", [], False))
    for case, title, args, opt_in in cases:
        print(f"== {title}")
        home = fresh_home(root, "home-" + case, opt_in=opt_in, start_in_ours=case == "autoload")
        log = os.path.join(home, "FreeCAD.log")
        code, out = run([FREECAD] + args + ["--log-file", log, os.path.join(HERE, "workbench_gui_check.py")],
                        environment(home, QT_QPA_PLATFORM="offscreen", WORKBENCH_CASE=case))
        shown = show(os.path.join(home, "workbench_gui_check.log"), out)
        check(f"{case}: the run ended well", shown and code == 0, f"exit code {code}")
        check_log(case, log, home, 2)
        print()

    print("== 7. the time the addon adds to FreeCAD's start")
    for name, opt_in, allowed in (("default", False, ["cadprompt", "cadprompt.workbench"]),
                                  ("ShowInTechDraw=True", True, ["cadprompt", "cadprompt.workbench", "hoischen",
                                                                 "hoischen.arrange", "hoischen.commands",
                                                                 "hoischen.layout", "hoischen.preset"])):
        home = fresh_home(root, "home-time-" + ("optin" if opt_in else "default"), addon=False, opt_in=opt_in)
        code, out = run([FREECAD, os.path.join(HERE, "workbench_startup_time.py")],
                        environment(home, QT_QPA_PLATFORM="offscreen"))
        report = os.path.join(home, "workbench_startup_time.log")
        text = open(report, encoding="utf-8").read() if os.path.isfile(report) else out[-2000:]
        ms = re.search(r"^startup_ms ([0-9.]+)$", text, re.M)
        mods = re.search(r"^modules ?(.*)$", text, re.M)
        loaded = mods.group(1).split() if mods else []
        if ms:
            print(f"      {name}: Init.py + InitGui.py took {float(ms.group(1)):.1f} ms and loaded {len(loaded)} modules: "
                  + " ".join(loaded))
        check(f"start, {name}: under {STARTUP_LIMIT_MS} ms",
              code == 0 and ms is not None and float(ms.group(1)) < STARTUP_LIMIT_MS, text[-1500:])
        check(f"start, {name}: loads only " + ", ".join(allowed) + " (no Qt import of ours, no network module)",
              code == 0 and loaded == allowed, " ".join(loaded))

    passed = COUNT[0] - len(FAILED)
    print(f"\n{passed} of {COUNT[0]} checks passed" + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
    print("ALL WORKBENCH TESTS PASSED" if not FAILED else "WORKBENCH TESTS FAILED")
    if temporary and not FAILED:
        shutil.rmtree(temporary, ignore_errors=True)     # removes the link in Mod/, not what it points to
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
