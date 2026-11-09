"""End-to-end check inside a running FreeCAD GUI (works offscreen too).

  QT_QPA_PLATFORM=offscreen freecad tests/gui_check.py      # writes the report to $HOISCHEN_REPORT
  or run it from FreeCAD's macro dialog / Python console.

Builds a stepped plate, a page, five dimensions in a messy spot, runs
Arrange, and checks every dimension LINE (where TechDraw drew the arrows)
against the book: 10 mm from the part, then 7 mm steps, small inside.
"""
import json, os, traceback
import FreeCAD as App, FreeCADGui as Gui

report = {"ok": False, "checks": [], "errors": []}
out_path = os.environ.get("HOISCHEN_REPORT", os.path.join(App.getUserAppDataDir(), "hoischen_gui_check.json"))

_trace = open(out_path + ".steps", "w")
def step(msg):
    _trace.write(msg + "\n"); _trace.flush()

def check(name, cond, detail=""):
    report["checks"].append({"name": name, "ok": bool(cond), "detail": detail})

try:
    from hoischen import arrange, preset
    from hoischen.commands import run_arrange, COMMANDS

    step('check("commands registered"')
    check("commands registered", all(c in Gui.listCommands() for c in COMMANDS))
    Gui.activateWorkbench("TechDrawWorkbench")
    Gui.updateGui()
    from PySide import QtGui
    texts = []
    for tb in Gui.getMainWindow().findChildren(QtGui.QToolBar):
        if tb.objectName() == "TechDraw Attributes":
            texts = [a.text() for a in tb.actions()]
    check("buttons on TechDraw Attributes toolbar", any("Hoischen" in t for t in texts), "; ".join(texts)[:300])

    step('changed = preset.apply_prefere')
    changed = preset.apply_preferences("0.5")
    p = App.ParamGet(preset.TD + "Dimensions")
    check("preset font 2.5", abs(p.GetFloat("FontSize") - 2.5) < 1e-9)
    check("preset line group 0.5", App.ParamGet(preset.TD + "Decorations").GetInt("LineGroup") == 2,
          str(App.ParamGet(preset.TD + "Decorations").GetInt("LineGroup")))
    check("preset ISO 128 lines", App.ParamGet(preset.TD + "Standards").GetInt("LineStandard") == 2)
    report["preset"] = changed

    step('doc = App.newDocument')
    doc = App.newDocument("HoischenCheck")
    # stepped plate seen from the front: 100 wide, 40 high, with a 30 x 15 notch top-left
    import Part
    base = Part.makeBox(100, 10, 40)
    notch = Part.makeBox(30, 10, 15, App.Vector(0, 0, 25))
    part = doc.addObject("Part::Feature", "Plate")
    part.Shape = base.cut(notch).removeSplitter()
    page = doc.addObject("TechDraw::DrawPage", "Page")
    tpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
    tpl.Template = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A3_Landscape_blank.svg")
    page.Template = tpl
    view = doc.addObject("TechDraw::DrawViewPart", "Front")
    page.addView(view)
    view.Source = [part]
    view.Direction = App.Vector(0, -1, 0)
    view.XDirection = App.Vector(1, 0, 0)
    scale = float(os.environ.get("HOISCHEN_SCALE", "1.0"))
    flip = os.environ.get("HOISCHEN_FLIP") == "1"   # start the labels below / left of the part
    view.ScaleType = "Custom"
    view.Scale = scale
    doc.recompute()
    Gui.updateGui()
    step('page.ViewObject.doubleClicked(')
    page.ViewObject.doubleClicked()   # open the page so TechDraw draws it
    Gui.updateGui()

    step('verts = view.getVisibleVertexe')
    verts = view.getVisibleVertexes()  # Y down
    def vid(x, y_up):
        x, y_up = x * scale, y_up * scale
        best = min(range(len(verts)), key=lambda i: (verts[i].x - x) ** 2 + (-verts[i].y - y_up) ** 2)
        return f"Vertex{best}"
    # part spans x -50..50, y -20..20 (model mm, view centre); notch: x -50..-20, y 5..20
    def dim(name, kind, a, b, x, y):
        d = doc.addObject("TechDraw::DrawViewDimension", name)
        d.Type = kind
        d.MeasureType = "Projected"
        d.References2D = [(view, vid(*a)), (view, vid(*b))]
        page.addView(d)
        d.X, d.Y = (-x, -y) if flip else (x, y)
        return d
    dims = [
        dim("Total", "DistanceX", (-50, -20), (50, -20), 3, 2),       # 100, drawn above, nonsense spot
        dim("Notch", "DistanceX", (-50, 5), (-20, 5), -1, 25),        # 30, above
        dim("Rest", "DistanceX", (-20, 20), (50, 20), 12, 40),        # 70, above, chains with Notch
        dim("Height", "DistanceY", (50, -20), (50, 20), 20, 3),       # 40, right
        dim("NotchH", "DistanceY", (-20, 5), (-20, 20), 30, -4),      # 15, right
    ]
    doc.recompute()
    Gui.updateGui()

    step('result = run_arrange')
    result = run_arrange(selection=[], page=page)
    report["arrange"] = result
    doc.recompute(); Gui.updateGui()
    import time
    from PySide import QtCore

    step('expect = {')
    # by the book, in paper mm from the view centre: 10 mm, then 7 mm steps
    top, right = 20 * scale, 50 * scale
    expect = {"Notch": top + 10, "Rest": top + 10, "Total": top + 17, "NotchH": right + 10, "Height": right + 17}
    if flip:
        expect = {k: -v for k, v in expect.items()}
    # Judge the result the way a reader would meet it: saved, closed, reopened,
    # drawn fresh. In-process read-backs can be stale.
    path = os.path.join(App.getUserAppDataDir(), "hoischen_check.FCStd")
    doc.saveAs(path)
    App.closeDocument(doc.Name)
    Gui.updateGui()
    doc = App.openDocument(path)
    for _ in range(100):                      # wait for the view geometry
        doc.recompute()
        if doc.getObject("Front").getVisibleEdges():
            break
        time.sleep(0.05)
    doc.recompute()
    page = doc.getObject("Page")
    page.ViewObject.doubleClicked()
    Gui.updateGui()
    doc.recompute()
    Gui.updateGui()
    dims = [doc.getObject(n) for n in ("Total", "Notch", "Rest", "Height", "NotchH")]
    for d in dims:
        axis = "x" if d.Type == "DistanceX" else "y"
        line = arrange.drawn_line(d, axis)
        target = expect[d.Name]
        detail = f"target {target}, drawn {line}, label X={d.X.Value:.2f} Y={d.Y.Value:.2f}, value {d.getRawValue():.2f}"
        check(f"{d.Name} line on its Hoischen line (scale {scale}{', below/left' if flip else ''})", line is not None and abs(line - target) < 0.05, detail)
    report["ok"] = all(c["ok"] for c in report["checks"])
except Exception:
    report["errors"].append(traceback.format_exc())

_trace.close()
with open(out_path, "w") as f:
    json.dump(report, f, indent=2, default=str)
if os.environ.get("HOISCHEN_EXIT"):
    os._exit(0 if report["ok"] else 1)
