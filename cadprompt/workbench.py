# SPDX-License-Identifier: MIT
"""What the CADprompt workbench needs before anything else is loaded.

InitGui.py imports this module when FreeCAD starts, so it stays small: no
panel, no engine, no hoischen and no TechDraw up here. Each command imports
its own code when the user runs it.

All buttons and menus live in the CADprompt workbench. The one exception is
the user's own choice: with the parameter ShowInTechDraw the drawing buttons
also go into TechDraw's toolbar (see set_show_in_techdraw).
"""

import FreeCAD as App
import FreeCADGui as Gui

from . import ICON

PARAMS = "User parameter:BaseApp/Preferences/Mod/CADprompt"   # the same group as paths.PARAMS

PANEL = "CADprompt_Panel"
# In the order of the work: set the drawing rules, then tidy the dimensions.
DRAWING = ("Hoischen_Preset05", "Hoischen_Preset07", "Hoischen_Arrange", "Hoischen_Settings")
TECHDRAW_TOOLBAR = "TechDraw Attributes"   # where TechDraw keeps its own Cascade tools
# The Weldment workbench that comes with this addon (weldment/, see weldment/VENDORED):
# weld, its symbols on the drawing, then the lists for the shop.
WELD = ("Weldment_FilletWeld", "Weldment_WeldSymbol", "Weldment_UpdateSymbols",
        "Weldment_WeldList", "Weldment_CutList")


class PanelCommand:
    def GetResources(self):
        return {"Pixmap": ICON, "MenuText": "CADprompt chat",
                "ToolTip": "Show or hide the CADprompt chat: say what to build, the AI builds it here"}

    def Activated(self):
        from . import panel
        panel.toggle()

    def IsActive(self):
        return True


def register_panel_command():
    if Gui.Command.get(PANEL) is None:
        Gui.addCommand(PANEL, PanelCommand())


def register_drawing_commands():
    """The Hoischen commands. Loads hoischen, so not at FreeCAD start by default."""
    if any(Gui.Command.get(name) is None for name in DRAWING):
        from hoischen import commands
        commands.register()


def weldment_source():
    """The folder that `import weldment` loads, and whether it is the copy in this addon.

    FreeCAD puts every Mod folder on sys.path. When the standalone Weldment
    workbench is installed as well, whichever comes first wins; both open the
    same models (the package has the same name), but only one version runs.
    """
    import os
    import weldment
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.dirname(os.path.abspath(weldment.__file__))
    return folder, os.path.normcase(os.path.dirname(folder)) == os.path.normcase(here)


def register_weld_commands():
    """The Weldment commands. Loads weldment and TechDraw, so not at FreeCAD start."""
    if all(Gui.Command.get(name) is not None for name in WELD):
        return
    from weldment import commands
    commands.register()
    folder, ours = weldment_source()
    if not ours:
        App.Console.PrintWarning(
            f"CADprompt: the welds come from {folder}, not from the copy in CADprompt. "
            "If you installed the Weldment workbench on its own, you can remove it.\n")


def show_panel():
    """Open the chat dock, or bring it back when it was closed. Never hides it."""
    from . import panel
    panel.show()


# ---- the drawing buttons in TechDraw: only when the user asks -----------------

class _TechDrawButtons:
    """Workbench manipulator: adds the drawing buttons to TechDraw and nowhere else.

    A manipulator can only add to a toolbar that exists, so the buttons join
    TechDraw's Attributes toolbar.
    """

    def modifyToolBars(self):
        # While a workbench is being activated it is not fully built yet, so
        # wb.name() raises here; the class name is already right.
        if type(Gui.activeWorkbench()).__name__ != "TechDrawWorkbench":
            return []
        return [{"append": name, "toolBar": TECHDRAW_TOOLBAR} for name in DRAWING]


_manipulator = None


def show_in_techdraw():
    return App.ParamGet(PARAMS).GetBool("ShowInTechDraw", False)


def _techdraw_buttons(on):
    global _manipulator
    if on == (_manipulator is not None):
        return
    if on:
        register_drawing_commands()
        _manipulator = _TechDrawButtons()
        Gui.addWorkbenchManipulator(_manipulator)
    else:
        Gui.removeWorkbenchManipulator(_manipulator)
        _manipulator = None
    try:
        wb = Gui.activeWorkbench()
    except Exception:      # FreeCAD is still starting: no workbench yet, nothing to rebuild
        return
    if type(wb).__name__ == "TechDrawWorkbench":
        wb.reloadActive()


def set_show_in_techdraw(on):
    """Show the drawing buttons in TechDraw's toolbar too, or take them out again.

    Saves the choice (parameter ShowInTechDraw) and applies it at once: when
    TechDraw is the workbench on screen its toolbars are rebuilt, otherwise
    the change shows the next time TechDraw is activated.
    """
    App.ParamGet(PARAMS).SetBool("ShowInTechDraw", bool(on))
    _techdraw_buttons(bool(on))


def startup():
    """Called once by InitGui.py. Keep it cheap: FreeCAD is still starting."""
    register_panel_command()
    if show_in_techdraw():
        _techdraw_buttons(True)
