# SPDX-License-Identifier: MIT
"""The addon in freecadcmd (no GUI): Init.py runs and does nothing. Started by test_workbench.py.

The report goes to <FREECAD_USER_HOME>/workbench_cmd_check.log.
"""

import os
import sys
import traceback

EARLY = sorted(m for m in sys.modules if m.split(".")[0] in ("cadprompt", "hoischen"))

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME = os.environ.get("FREECAD_USER_HOME") or ""
LOG = os.path.join(HOME or ".", "workbench_cmd_check.log")
FAILED = []
COUNT = [0]


def say(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    say(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def main():
    import FreeCAD as App

    say("-- freecadcmd --")
    mod = os.path.join(HOME, "Mod", "CADprompt")
    dirs = [d for d in App.__ModDirs__ if os.path.realpath(d) == os.path.realpath(REPO)]
    check("(setup) no GUI, the throw-away profile is in use and FreeCAD found the addon in its Mod folder",
          not App.GuiUp and os.path.realpath(App.getUserAppDataDir()) == os.path.realpath(HOME)
          and len(dirs) == 1 and os.path.abspath(dirs[0]) == os.path.abspath(mod), f"{App.GuiUp} {dirs}")
    check("FreeCAD does not count the addon as failed",
          "cadprompt" not in getattr(App, "__failed_mods__", []), str(getattr(App, "__failed_mods__", [])))
    check("Init.py loaded nothing of the addon", EARLY == [], str(EARLY))
    try:
        import cadprompt
        ok = os.path.isfile(cadprompt.ICON) and sorted(m for m in sys.modules if m.startswith("cadprompt")) == ["cadprompt"]
        detail = cadprompt.__file__
    except Exception:
        ok, detail = False, traceback.format_exc()
    check("the cadprompt package can be imported from there, and that alone loads nothing more", ok, detail)


if not HOME:
    os._exit(2)      # this test must not touch the user's FreeCAD settings
open(LOG, "w").close()
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
