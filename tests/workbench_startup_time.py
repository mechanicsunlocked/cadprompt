# SPDX-License-Identifier: MIT
"""How long the addon keeps FreeCAD's start waiting. Started by test_workbench.py.

Runs in a FreeCAD whose profile does NOT hold the addon, and then does what
FreeCAD's start does with an addon folder: Init.py and InitGui.py, each with
exec() inside a function. Nothing of the addon is loaded before, so the time
includes every import. ShowInTechDraw is taken from the profile as it is.

Writes <FREECAD_USER_HOME>/workbench_startup_time.log:
    one line "startup_ms <number>", one line "modules <names>".
"""

import os
import sys
import time
import traceback

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME = os.environ.get("FREECAD_USER_HOME") or ""
LOG = os.path.join(HOME or ".", "workbench_startup_time.log")


def run_file(path):
    with open(path, "rt", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"))


def main():
    before = set(sys.modules)
    if [m for m in before if m.split(".")[0] in ("cadprompt", "hoischen")]:
        raise RuntimeError("the addon is already loaded: this profile must not hold it")
    sys.path.insert(0, REPO)          # FreeCAD puts every addon folder on the path before it starts them
    start = time.perf_counter()
    run_file(os.path.join(REPO, "Init.py"))
    run_file(os.path.join(REPO, "InitGui.py"))
    ms = (time.perf_counter() - start) * 1000.0
    import FreeCADGui as Gui
    if "CADpromptWorkbench" not in Gui.listWorkbenches():
        raise RuntimeError("InitGui.py did not add the workbench")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(f"startup_ms {ms:.2f}\n")
        f.write("modules " + " ".join(sorted(set(sys.modules) - before)) + "\n")


if not HOME:
    os._exit(2)      # this test must not touch the user's FreeCAD settings
try:
    main()
except Exception:
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(traceback.format_exc())
    os._exit(1)
os._exit(0)
