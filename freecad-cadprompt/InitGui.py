# CADprompt: a chat panel that talks to the user's own Claude Code.
# Adds a CADprompt button to the File toolbar of every workbench and an entry
# in the Tools menu; both show or hide the dock. No workbench of its own.

# FreeCAD runs this file with exec(): module-level names are gone by the time
# the methods below are called, so every method imports what it needs.

import FreeCAD as App
import FreeCADGui as Gui


class _PanelCommand:
    def GetResources(self):
        from cadprompt import ICON
        return {"Pixmap": ICON, "MenuText": "CADprompt",
                "ToolTip": "Show or hide the CADprompt chat: say what to build, Claude builds it here"}

    def Activated(self):
        from cadprompt import panel
        panel.toggle()

    def IsActive(self):
        return True


class _Manipulator:
    def modifyToolBars(self):
        return [{"append": "CADprompt_Panel", "toolBar": "File"}]

    def modifyMenuBar(self):
        return [{"append": "CADprompt_Panel", "menuItem": "Std_DlgCustomize"}]


try:
    Gui.addCommand("CADprompt_Panel", _PanelCommand())
    Gui.addWorkbenchManipulator(_Manipulator())
except Exception as _exc:  # never take FreeCAD's start-up down with us
    App.Console.PrintError(f"CADprompt addon failed to load: {_exc}\n")
