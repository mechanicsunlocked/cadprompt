# Hoischen for TechDraw

TechDraw drawings the way the book says: Hoischen, *Technisches Zeichnen*,
DIN 406-11, ISO 128 and ISO 129-1. A FreeCAD 1.x addon; no workbench of its
own, it adds four buttons to TechDraw's Attributes toolbar, next to the Cascade
tools.

**Arrange dimensions.** Select dimensions, a view, or nothing (whole page).
Every horizontal and vertical dimension goes on its line: the first line
**10 mm** from the part, every further line **7 mm** out, small dimensions
inside and overall dimensions outside so extension lines never cross a
dimension line, and dimensions that sit side by side share a line (chain).
Millimetres on the paper, so it looks the same at 1:5 and 2:1. Dimensions
stay on the side of the part you put them on.

**DIN preset, line group 0.5 / 0.7.** Sets TechDraw's defaults to the book and
restyles the dimensions already in the document:

| | group 0.5 (A4 to A2) | group 0.7 (A1, A0) |
|---|---|---|
| lines wide / narrow | 0.5 / 0.25 mm | 0.7 / 0.35 mm |
| dimension text (10 x narrow line, ISO 129-1) | 2.5 mm | 3.5 mm |
| arrowheads, filled | 2.5 mm | 3.5 mm |
| extension lines | start at the edge, run 2 mm past the line | 2.8 mm past |

Also: ISO oriented text (above the line, readable from bottom and right), no
units, ISO 128-20 line types, first-angle projection, view labels 3.5 / 5 mm,
and the German ISO 5457 sheet with ISO 7200 title block as default template.

**Dimension spacing...** changes the 10 / 7 mm if your school does it
differently.

From Python (or for an AI agent driving FreeCAD):

```python
from hoischen import preset
from hoischen.commands import run_arrange
preset.apply_preferences("0.5")
run_arrange(selection=[], page=App.ActiveDocument.getObject("Page"))
```

## Install

`../setup install` links this folder into FreeCAD's `Mod` as `Hoischen`. By
hand: copy or link it there (`~/.local/share/FreeCAD/v1-1/Mod/Hoischen`, or
for the Flatpak `~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/Hoischen`)
and restart FreeCAD.

## Tests

- `python -m unittest discover tests`: the layout rules, any Python.
- `tests/gui_check.py`: end to end in FreeCAD (macro dialog, or
  `QT_QPA_PLATFORM=offscreen freecad tests/gui_check.py`): builds a stepped
  plate, five dimensions in odd places, arranges them, saves, reopens, and
  checks every drawn dimension line against the book. `HOISCHEN_SCALE=0.5`
  and `HOISCHEN_FLIP=1` test other scales and the other sides.

Checked on FreeCAD 1.1.3 at 1:2, 1:1 and 2:1, all four sides: every line within
0.002 mm.

## Notes

- Only horizontal and vertical dimensions are arranged; oblique, radius,
  diameter and angle dimensions are left alone.
- The text box height factor (0.838 x font size for osifont, measured) is in
  the parameter `Mod/Hoischen/TextBoxFactor` if you use another font.
- Arranged dimensions get `AllowSnap = False`, so TechDraw's own snapping does
  not pull them off their lines.
