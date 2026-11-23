---
description: Weld the parts that touch, with fillet welds per ISO 2553
argument-hint: "[a4] [both sides] [intermittent 3 x 50 (25)] [which joints]"
---
Weld the open model as the Welding section of CLAUDE.md describes. First
call `find_joints` and show the joints as a short list: the two parts, the
seam length, and the weld you propose for each (throat `a` from the wall
thicknesses, whole seam or intermittent, one or both sides). Leave out the
joints that are welded already. Then ask once whether to weld them like that,
and weld the ones the user agrees to with `make_weld`, one call for each.
End with the total welded length and the weld metal. Wishes: $ARGUMENTS
