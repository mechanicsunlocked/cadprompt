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
