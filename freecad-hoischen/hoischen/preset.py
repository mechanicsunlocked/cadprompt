"""Hoischen / DIN preset for TechDraw.

Writes TechDraw's own preferences, so every new page, view and dimension
starts out right, and can restyle the dimensions already in a document.

Line groups as Hoischen gives them (DIN ISO 128, lettering per ISO 129-1:
height = 10 x the narrow line width, "mit Wahl der Liniengruppe ist auch die
Schriftgroesse festgelegt"):

    group  wide   narrow  lettering h   use
    0.5    0.5    0.25    2.5 mm        A4 to A2 (preferred)
    0.7    0.7    0.35    3.5 mm        A1 and A0

Arrowhead length equals the lettering height (TechDraw's own ISO 129 rule).
Extension lines start at the body edge (no gap) and run past the dimension
line by 8 x the narrow width, which TechDraw does by itself (2 mm in group 0.5).
"""

import os

import FreeCAD as App

TD = "User parameter:BaseApp/Preferences/Mod/TechDraw/"
OWN = "User parameter:BaseApp/Preferences/Mod/Hoischen"

GROUPS = {
    "0.5": {"lg_name": "FC 0.50mm", "narrow": 0.25, "text": 2.5, "label": 3.5},
    "0.7": {"lg_name": "FC 0.70mm", "narrow": 0.35, "text": 3.5, "label": 5.0},
}


def _param(group):
    return App.ParamGet(TD + group)


def _line_group_index(name):
    """Index of a line group in TechDraw's LineGroup.csv (the combo box index)."""
    path = _param("Files").GetString(
        "LineGroupFile", os.path.join(App.getResourceDir(), "Mod", "TechDraw", "LineGroup", "LineGroup.csv")
    )
    try:
        with open(path, encoding="utf-8") as f:
            names = [line[1:].split(",")[0].strip() for line in f if line.startswith("*")]
        return names.index(name)
    except (OSError, ValueError):
        return None


def _iso_line_standard_index():
    """Index of ISO 128-20 among TechDraw's line standards (sorted LineDef files)."""
    folder = _param("Files").GetString(
        "LineDefLocation", os.path.join(App.getResourceDir(), "Mod", "TechDraw", "LineGroup", "")
    )
    try:
        names = sorted(f.split(".LineDef")[0] for f in os.listdir(folder) if ".LineDef" in f)
    except OSError:
        return None
    for i, name in enumerate(names):
        if name.startswith("ISO.128"):
            return i
    return None


def _german_iso_template(fmt="A3_Landscape"):
    """The German ISO 5457 / ISO 7200 template FreeCAD ships, if this install has it."""
    base = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO")
    for path in (
        os.path.join(base, "localized", "de", f"{fmt}_ISO7200_DE.svg"),            # FreeCAD 1.1
        os.path.join(base, "localized", "de", f"{fmt}_ISO5457_advanced.svg"),      # older layouts
        os.path.join(base, f"{fmt}_ISO5457_advanced.svg"),                         # English fallback
    ):
        if os.path.isfile(path):
            return path
    return None


def apply_preferences(group="0.5", template_format="A3_Landscape"):
    """Set TechDraw's defaults to the book. Returns a list of what was changed."""
    g = GROUPS[group]
    done = []

    dims = _param("Dimensions")
    dims.SetInt("StandardAndStyle", 0)          # ISO oriented: text above the line, readable from bottom and right
    dims.SetFloat("FontSize", g["text"])
    dims.SetFloat("ArrowSize", g["text"])
    dims.SetInt("ArrowStyle", 0)                # filled arrow
    dims.SetBool("ShowUnits", False)            # mm is implied on a DIN drawing
    dims.SetFloat("GapISO", 0.0)                # extension lines start at the body edge
    dims.SetString("DiameterSymbol", "⌀")
    done.append(f"dimensions: ISO oriented, text and arrows {g['text']} mm, no units, no extension gap")

    idx = _line_group_index(g["lg_name"])
    if idx is not None:
        _param("Decorations").SetInt("LineGroup", idx)
        done.append(f"line group {group} (wide {group} mm, narrow {g['narrow']} mm)")

    idx = _iso_line_standard_index()
    if idx is not None:
        _param("Standards").SetInt("LineStandard", idx)
        done.append("line types per ISO 128-20")
    _param("Standards").SetBool("EnforceISODate", True)

    _param("General").SetInt("ProjectionAngle", 0)   # first angle, projection method 1 (European)
    done.append("first-angle projection")

    _param("Labels").SetFloat("LabelSize", g["label"])
    done.append(f"view labels {g['label']} mm")

    tpl = _german_iso_template(template_format)
    if tpl:
        _param("Files").SetString("TemplateFile", tpl)
        done.append(f"default template {os.path.basename(tpl)} (ISO 5457 frame, ISO 7200 title block)")

    own = App.ParamGet(OWN)
    own.SetString("LineGroup", group)
    return done


def restyle_dimensions(doc, group="0.5"):
    """Give existing dimensions in a document the preset's look. Needs the GUI."""
    if not App.GuiUp or doc is None:
        return 0
    g = GROUPS[group]
    count = 0
    for obj in doc.Objects:
        if not obj.isDerivedFrom("TechDraw::DrawViewDimension"):
            continue
        vo = getattr(obj, "ViewObject", None)
        if vo is None:
            continue
        vo.StandardAndStyle = "ISO Oriented"
        vo.Fontsize = g["text"]
        vo.Arrowsize = g["text"]
        vo.LineWidth = g["narrow"]
        vo.GapFactorISO = 0.0
        count += 1
    return count
