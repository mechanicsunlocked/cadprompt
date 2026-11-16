# SPDX-License-Identifier: MIT
"""How long the addon keeps FreeCAD's start waiting. Started by test_workbench.py.

Runs in a FreeCAD whose profile does NOT hold the addon, and then does what
FreeCAD's start does with an addon folder: Init.py and InitGui.py, each with
exec() inside a function. Nothing of the addon is loaded before, so the time
includes every import. ShowInTechDraw is taken from the profile as it is.

Before that it does the same with two workbenches that do nothing, made in
the profile: the first takes what FreeCAD does once (the first icon, the first
command), the second is the baseline. A slow computer is slow for the baseline
too, so the test compares the two times and needs no fixed number.

Writes <FREECAD_USER_HOME>/workbench_startup_time.log:
    one line "baseline_ms <number>", one line "startup_ms <number>", one line "modules <names>".
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


# A workbench with the parts ours has at the start: a package with one module,
# one command with an icon, one workbench. {n} makes two of them.
NOTHING_INIT_GUI = """import FreeCAD as App
import FreeCADGui as Gui


class Nothing{n}Workbench(Gui.Workbench):
    MenuText = "Nothing {n}"
    ToolTip = "A workbench that does nothing"

    def __init__(self):
        from nothing{n} import ICON
        self.__class__.Icon = ICON

    def GetClassName(self):
        return "Gui::PythonWorkbench"


try:
    from nothing{n} import workbench as _workbench

    _workbench.startup()
    Gui.addWorkbench(Nothing{n}Workbench())
except Exception as _exc:
    App.Console.PrintError(f"nothing{n} failed to load: {{_exc}}\\n")
"""
NOTHING_WORKBENCH = """import FreeCAD as App
import FreeCADGui as Gui

from . import ICON


class Command:
    def GetResources(self):
        return {{"Pixmap": ICON, "MenuText": "Nothing {n}", "ToolTip": "Does nothing"}}

    def Activated(self):
        pass

    def IsActive(self):
        return True


def startup():
    if Gui.Command.get("Nothing{n}_Command") is None:
        Gui.addCommand("Nothing{n}_Command", Command())
    App.ParamGet("User parameter:BaseApp/Preferences/Mod/Nothing{n}").GetBool("ShowInTechDraw", False)
"""


def run_file(path):
    with open(path, "rt", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"))


def start_addon(folder):
    """What FreeCAD's start does with an addon folder. Returns the milliseconds it took."""
    sys.path.insert(0, folder)        # FreeCAD puts every addon folder on the path before it starts them
    start = time.perf_counter()
    run_file(os.path.join(folder, "Init.py"))
    run_file(os.path.join(folder, "InitGui.py"))
    return (time.perf_counter() - start) * 1000.0


def nothing(n):
    """Make the workbench that does nothing, number n, in the profile."""
    import shutil
    folder = os.path.join(HOME, f"nothing{n}-addon")
    os.makedirs(os.path.join(folder, f"nothing{n}"))
    icon = os.path.join(folder, "nothing.svg")
    shutil.copyfile(os.path.join(REPO, "icons", "cadprompt.svg"), icon)
    for name, text in (("Init.py", "# nothing to do without the GUI\n"),
                       ("InitGui.py", NOTHING_INIT_GUI.format(n=n)),
                       (os.path.join(f"nothing{n}", "__init__.py"), f"ICON = {icon!r}\n"),
                       (os.path.join(f"nothing{n}", "workbench.py"), NOTHING_WORKBENCH.format(n=n))):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)
    return folder


def main():
    import FreeCADGui as Gui
    first, second = nothing(1), nothing(2)
    start_addon(first)                # not counted: it pays for what FreeCAD does only once
    baseline = start_addon(second)
    if not {"Nothing1Workbench", "Nothing2Workbench"} <= set(Gui.listWorkbenches()):
        raise RuntimeError("the workbenches that do nothing were not added")
    before = set(sys.modules)
    if [m for m in before if m.split(".")[0] in ("cadprompt", "hoischen")]:
        raise RuntimeError("the addon is already loaded: this profile must not hold it")
    ms = start_addon(REPO)
    if "CADpromptWorkbench" not in Gui.listWorkbenches():
        raise RuntimeError("InitGui.py did not add the workbench")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(f"baseline_ms {baseline:.2f}\n")
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
