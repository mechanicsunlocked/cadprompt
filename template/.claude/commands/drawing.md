---
description: DIN workshop drawing of the current part (Hoischen style)
argument-hint: "[A5|A4|A3|A2] [extra wishes]"
---
Make a workshop drawing of the current part following the Drawings section
of CLAUDE.md: DIN preset, the views a machinist needs, functional dimensions,
`run_arrange` for the spacing, title block filled, PDF exported to
`drawings/`. Sheet size and wishes: $ARGUMENTS
FreeCAD ships ISO 5457 templates only from A4 up. For A5 (210 x 148 mm,
landscape), make the template from the A4 ISO one: same frame and title
block style, scaled to the A5 sheet, saved in `drawings/templates/`, and say
that A5 is outside ISO 5457.
Afterwards list anything you were unsure about (a tolerance, a surface,
a missing material) so he can decide.
