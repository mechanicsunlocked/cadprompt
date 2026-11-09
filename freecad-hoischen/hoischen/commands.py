"""FreeCAD commands: Arrange, the two presets, and the spacing settings."""

import os

import FreeCAD as App
import FreeCADGui as Gui

from . import arrange, preset

ICONS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")


def _active_page():
    view = Gui.activeView()
    getter = getattr(view, "getPage", None)
    if getter:
        try:
            return getter()
        except Exception:
            pass
    doc = App.ActiveDocument
    pages = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")] if doc else []
    return pages[0] if len(pages) == 1 else None


def _say(text):
    App.Console.PrintMessage(f"Hoischen: {text}\n")


def run_arrange(selection=None, page=None):
    """Arrange dimensions; usable from the GUI command, the console, or an agent."""
    doc = App.ActiveDocument
    if doc is None:
        return {"placed": 0, "skipped": ["no document"]}
    if selection is None:
        selection = [s.Object for s in Gui.Selection.getSelectionEx()] if App.GuiUp else []
    if page is None and App.GuiUp:
        page = _active_page()
    dims = arrange.dimensions_for(selection, page)
    if not dims:
        return {"placed": 0, "skipped": ["no horizontal or vertical dimensions found"]}

    def redraw():
        # The page moves a label only while its X/Y are still "touched", and a
        # recompute clears that flag; so let the page see the change first.
        if App.GuiUp:
            Gui.updateGui()
        doc.recompute()
        if App.GuiUp:
            Gui.updateGui()

    doc.openTransaction("Arrange dimensions (Hoischen)")
    try:
        placed, skipped = arrange.plan(dims)
        arrange.apply(placed)
        redraw()
    except Exception:
        doc.abortTransaction()   # nothing half-moved stays behind
        raise
    doc.commitTransaction()
    return {"placed": len(placed), "skipped": skipped}


class Arrange:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "hoischen_arrange.svg"),
            "MenuText": "Arrange dimensions (Hoischen)",
            "ToolTip": (
                "Puts horizontal and vertical dimensions on their lines per DIN 406-11 / Hoischen:\n"
                "first line 10 mm from the part, every further line 7 mm out, small dimensions inside.\n"
                "Select dimensions, a view, or nothing for the whole page."
            ),
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        r = run_arrange()
        _say(f"{r['placed']} dimensions placed")
        for msg in r["skipped"]:
            _say(f"skipped: {msg}")


class Preset:
    def __init__(self, group):
        self.group = group

    def GetResources(self):
        g = preset.GROUPS[self.group]
        sheets = "A4 to A2" if self.group == "0.5" else "A1 and A0"
        return {
            "Pixmap": os.path.join(ICONS, "hoischen_preset.svg"),
            "MenuText": f"DIN preset, line group {self.group} ({sheets})",
            "ToolTip": (
                f"Sets TechDraw to the book for line group {self.group}: lines {self.group}/{g['narrow']} mm, "
                f"text and arrows {g['text']} mm, ISO oriented, first-angle projection, ISO 128 line types,\n"
                "German ISO 5457 template. Also restyles the dimensions already in this document."
            ),
        }

    def IsActive(self):
        return True

    def Activated(self):
        for line in preset.apply_preferences(self.group):
            _say(line)
        n = preset.restyle_dimensions(App.ActiveDocument, self.group)
        if n:
            _say(f"{n} existing dimensions restyled")


class Settings:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "hoischen_preset.svg"),
            "MenuText": "Dimension spacing...",
            "ToolTip": "First line distance and line pitch used by Arrange (10 mm and 7 mm by the book)",
        }

    def IsActive(self):
        return True

    def Activated(self):
        from PySide import QtGui

        first, pitch = arrange.spacing()
        win = Gui.getMainWindow()
        first, ok = QtGui.QInputDialog.getDouble(win, "Hoischen", "Body edge to first dimension line (mm)", first, 1.0, 50.0, 1)
        if not ok:
            return
        pitch, ok = QtGui.QInputDialog.getDouble(win, "Hoischen", "Between parallel dimension lines (mm)", pitch, 1.0, 50.0, 1)
        if not ok:
            return
        p = App.ParamGet(arrange.OWN)
        p.SetFloat("FirstOffset", first)
        p.SetFloat("Pitch", pitch)
        _say(f"spacing {first} / {pitch} mm")


COMMANDS = ("Hoischen_Arrange", "Hoischen_Preset05", "Hoischen_Preset07", "Hoischen_Settings")


def register():
    Gui.addCommand("Hoischen_Arrange", Arrange())
    Gui.addCommand("Hoischen_Preset05", Preset("0.5"))
    Gui.addCommand("Hoischen_Preset07", Preset("0.7"))
    Gui.addCommand("Hoischen_Settings", Settings())
