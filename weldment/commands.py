"""FreeCAD commands: Fillet weld, Weld symbol, Update symbols."""

import os

import FreeCAD as App
import FreeCADGui as Gui

from . import drawing, feature, lists

ICONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")


def _say(text):
    App.Console.PrintMessage(f"Weldment: {text}\n")


def _warn(text):
    App.Console.PrintWarning(f"Weldment: {text}\n")


def _recompute_error(obj):
    """Why `obj`'s recompute failed: FreeCAD keeps the exception text as the status string."""
    status = obj.getStatusString() if hasattr(obj, "getStatusString") else ""
    if status and status not in ("Valid", "Touched"):
        return status
    return ", ".join(s for s in obj.State if s != "Touched")


def selected_faces():
    """[(object, 'FaceN'), ...] from the current selection, in selection order."""
    faces = []
    for sel in Gui.Selection.getSelectionEx("", 0):
        for sub in sel.SubElementNames:
            if sub.split(".")[-1].startswith("Face"):
                faces.append((sel.Object, sub))
    return faces


def run_fillet_weld(faces=None, throat=4.0, **props):
    """Make a fillet weld from two picked faces; usable from the GUI, the console, or an agent."""
    doc = App.ActiveDocument
    if doc is None:
        raise ValueError("no document")
    if faces is None:
        faces = selected_faces()
    if len(faces) != 2:
        raise ValueError("select exactly two faces: one on each part, along the edge where they meet")
    doc.openTransaction("Fillet weld")
    try:
        weld = feature.make_fillet_weld(doc, faces[0], faces[1], throat=throat, **props)
        doc.recompute()
        if weld.Shape.isNull() or not weld.Shape.isValid():
            raise ValueError(_recompute_error(weld) or "weld could not be built")
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return weld


def _active_page_and_view():
    """(page, view): a selected DrawViewPart, or the only part view of the active page."""
    for sel in Gui.Selection.getSelection():
        if sel.isDerivedFrom("TechDraw::DrawViewPart"):
            pages = [o for o in sel.InList if o.isDerivedFrom("TechDraw::DrawPage")]
            return (pages[0] if pages else None), sel
    page = None
    view = Gui.activeView()
    getter = getattr(view, "getPage", None)
    if getter:
        try:
            page = getter()
        except Exception:
            page = None
    if page is None:
        pages = [o for o in App.ActiveDocument.Objects if o.isDerivedFrom("TechDraw::DrawPage")]
        page = pages[0] if len(pages) == 1 else None
    if page is None:
        return None, None
    parts = [v for v in page.Views if v.isDerivedFrom("TechDraw::DrawViewPart")]
    return page, (parts[0] if len(parts) == 1 else None)


def run_weld_symbol(welds=None, view=None, page=None):
    doc = App.ActiveDocument
    if doc is None:
        raise ValueError("no document")
    if welds is None:
        welds = [o for o in Gui.Selection.getSelection() if o in feature.all_welds(doc)]
    if not welds:
        welds = feature.all_welds(doc)
    if not welds:
        raise ValueError("no welds in the document")
    if view is None:
        page, view = _active_page_and_view()
    if view is None:
        raise ValueError("select the drawing view (or have one part view on the active page)")
    welds = [w for w in welds if w.FaceA[0] in view.Source or w.FaceB[0] in view.Source] or welds
    doc.openTransaction("Weld symbols")
    try:
        made = [drawing.add_symbol(w, view, page) for w in welds]
        doc.recompute()
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return made


class FilletWeldCmd:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "weldment_fillet.svg"),
            "MenuText": "Fillet weld",
            "ToolTip": "Select two faces that meet along an edge: a parametric fillet bead, throat a = 4 mm,\n"
                       "over the whole seam. Change Throat, Length, Offset or the intermittent values afterwards.",
        }

    def IsActive(self):
        return App.ActiveDocument is not None and len(selected_faces()) == 2

    def Activated(self):
        try:
            weld = run_fillet_weld()
        except Exception as exc:
            _warn(str(exc))
            return
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(weld)
        _say(f"{weld.Label}: a{weld.Throat.Value:g}, {weld.WeldLength.Value:.0f} mm, "
             f"{weld.DepositMass * 1000:.0f} g weld metal")


class WeldSymbolCmd:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "weldment_symbol.svg"),
            "MenuText": "Weld symbols on drawing",
            "ToolTip": "Puts the ISO 2553 symbol of the selected welds (or all welds) on the selected view\n"
                       "(or the only part view of the active page).",
        }

    def IsActive(self):
        return App.ActiveDocument is not None and bool(feature.all_welds(App.ActiveDocument))

    def Activated(self):
        try:
            made = run_weld_symbol()
        except Exception as exc:
            _warn(str(exc))
            return
        _say(f"{len(made)} weld symbol(s) placed")


class UpdateSymbolsCmd:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "weldment_update.svg"),
            "MenuText": "Update weld symbols",
            "ToolTip": "Re-reads every weld into its drawing symbols (texts and arrow position).",
        }

    def IsActive(self):
        return App.ActiveDocument is not None and bool(drawing.symbols_of(App.ActiveDocument))

    def Activated(self):
        doc = App.ActiveDocument
        doc.openTransaction("Update weld symbols")
        try:
            n = drawing.update_symbols(doc)
            doc.recompute()
        except Exception as exc:
            doc.abortTransaction()
            _warn(str(exc))
            return
        doc.commitTransaction()
        _say(f"{n} weld symbol(s) updated")


def run_list(kind, members=None):
    """Weld list or cut list: spreadsheet in the document; a table on the active page if there is one."""
    doc = App.ActiveDocument
    if doc is None:
        raise ValueError("no document")
    doc.openTransaction("Weld list" if kind == "weld" else "Cut list")
    try:
        sheet = lists.make_weld_list(doc) if kind == "weld" else lists.make_cut_list(doc, members)
        page, _ = _active_page_and_view()
        table = None
        if page is not None:
            old = [v for v in page.Views if v.isDerivedFrom("TechDraw::DrawViewSpreadsheet") and v.Source == sheet]
            table = old[0] if old else lists.add_table(page, sheet)
        doc.recompute()
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return sheet, table


class WeldListCmd:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "weldment_weldlist.svg"),
            "MenuText": "Weld list",
            "ToolTip": "Spreadsheet WeldList: every weld with a, z, length, pattern, sides and weld metal;\n"
                       "also a table on the active drawing page.",
        }

    def IsActive(self):
        return App.ActiveDocument is not None and bool(feature.all_welds(App.ActiveDocument))

    def Activated(self):
        try:
            sheet, table = run_list("weld")
        except Exception as exc:
            _warn(str(exc))
            return
        _say(f"{sheet.Label} written" + (f", table on {table.Label}" if table else ""))


class CutListCmd:
    def GetResources(self):
        return {
            "Pixmap": os.path.join(ICONS, "weldment_cutlist.svg"),
            "MenuText": "Cut list",
            "ToolTip": "Spreadsheet CutList: the selected parts (or every part a weld touches) by profile,\n"
                       "length and end angles, with quantities; also a table on the active drawing page.",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        members = [o for o in Gui.Selection.getSelection() if hasattr(o, "Shape")] or None
        try:
            sheet, table = run_list("cut", members)
        except Exception as exc:
            _warn(str(exc))
            return
        _say(f"{sheet.Label} written" + (f", table on {table.Label}" if table else ""))


COMMANDS = ["Weldment_FilletWeld", "Weldment_WeldSymbol", "Weldment_UpdateSymbols", "Weldment_WeldList", "Weldment_CutList"]


def register():
    Gui.addCommand("Weldment_FilletWeld", FilletWeldCmd())
    Gui.addCommand("Weldment_WeldSymbol", WeldSymbolCmd())
    Gui.addCommand("Weldment_UpdateSymbols", UpdateSymbolsCmd())
    Gui.addCommand("Weldment_WeldList", WeldListCmd())
    Gui.addCommand("Weldment_CutList", CutListCmd())
