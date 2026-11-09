# Hoischen for TechDraw: drawings the way the book says.
# Adds its buttons to TechDraw's Attributes toolbar, next to the Cascade tools
# (no workbench of its own).

import FreeCAD as App
import FreeCADGui as Gui


class _HoischenToolbar:
    """Workbench manipulator: our buttons go into TechDraw and nowhere else.

    A manipulator can only add to toolbars that exist, so they join the
    Attributes toolbar, where TechDraw keeps its own Cascade tools.
    """

    def modifyToolBars(self):
        # While a workbench is being activated it is not fully built yet, so
        # wb.name() raises here; the class name is already right.
        if type(Gui.activeWorkbench()).__name__ != "TechDrawWorkbench":
            return []
        from hoischen.commands import COMMANDS

        return [{"append": c, "toolBar": "TechDraw Attributes"} for c in COMMANDS]


try:
    from hoischen import commands as _hoischen_commands

    _hoischen_commands.register()
    Gui.addWorkbenchManipulator(_HoischenToolbar())
except Exception as _exc:  # never take FreeCAD's start-up down with us
    App.Console.PrintError(f"Hoischen addon failed to load: {_exc}\n")
