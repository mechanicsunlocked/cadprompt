# SPDX-License-Identifier: MIT
# CADprompt: a workbench with the chat panel and the DIN drawing tools.
# All its buttons and menus are in this workbench; nothing is added to other
# workbenches unless the user asks for it (cadprompt/workbench.py). The chat
# dock stays open when the user changes to another workbench.

# FreeCAD runs this file with exec() inside a function: names defined here are
# not visible in the class body and are gone by the time the methods are
# called, so every method imports what it needs.

import FreeCAD as App
import FreeCADGui as Gui


class CADpromptWorkbench(Gui.Workbench):
    MenuText = "CADprompt"
    ToolTip = "Say what to build and the AI builds it in FreeCAD; with tools for DIN workshop drawings"

    def __init__(self):
        from cadprompt import ICON
        self.__class__.Icon = ICON

    def Initialize(self):
        # Runs the first time the user opens this workbench, not at FreeCAD start.
        from cadprompt import workbench
        workbench.register_drawing_commands()
        drawing = list(workbench.DRAWING)
        self.appendToolbar("CADprompt", [workbench.PANEL])
        self.appendToolbar("Drawing", drawing)
        self.appendMenu("CADprompt", [workbench.PANEL, "Separator"])
        self.appendMenu(["CADprompt", "Drawing"], drawing)
        try:
            workbench.register_weld_commands()
        except Exception as exc:     # no TechDraw, a broken copy: the chat must still work
            import FreeCAD as App
            App.Console.PrintError(f"CADprompt: the weld tools did not load: {exc}\n")
        else:
            weld = list(workbench.WELD)
            self.appendToolbar("Weld", weld)
            self.appendMenu(["CADprompt", "Weld"], weld)

    def Activated(self):
        from cadprompt import workbench
        workbench.show_panel()

    def Deactivated(self):
        pass    # the chat stays open in every workbench

    def GetClassName(self):
        return "Gui::PythonWorkbench"


try:
    from cadprompt import workbench as _workbench

    _workbench.startup()
    Gui.addWorkbench(CADpromptWorkbench())
except Exception as _exc:  # never take FreeCAD's start-up down with us
    App.Console.PrintError(f"CADprompt addon failed to load: {_exc}\n")
