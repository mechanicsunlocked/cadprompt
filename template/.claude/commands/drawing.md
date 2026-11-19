---
description: DIN workshop drawing of the current part (Hoischen style)
argument-hint: "[A5|A4|A3|A2] [extra wishes]"
---
Make a workshop drawing of the current part following the Drawings section
of CLAUDE.md: call `make_drawing` first, read its report, then add by hand
with `run_python` what the report lists as missing and what he asks for
here, `run_arrange` for the spacing, PDF exported to `drawings/` again.
Sheet size and wishes: $ARGUMENTS
FreeCAD ships ISO 5457 templates only from A4 up. For A5, `make_drawing`
uses A4 and says so.
Afterwards list anything you were unsure about (a tolerance, a surface,
a missing material) so he can decide.
