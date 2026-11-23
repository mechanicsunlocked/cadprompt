---
description: Check the model like a reviewer before it goes to the shop
---
Check the open model with `get_document_state` and `run_python`: recompute
errors, invalid shapes, unconstrained sketches, hard-coded sizes that should
be parameters, features that would break if a parameter changes (in one
`run_python` call for each Params value: set it 10 % larger, recompute, note
what breaks, set it back, recompute), mass in steel (7.85 g/cm3) or the
material he names. For a welded part also: every weld recomputes, its throat
`a` fits the walls (about 0.7 x the thinner one, at least 3 mm), and
`find_joints` shows no joint left without a weld that looks like it needs one.
Report findings, most serious first. Change nothing.
$ARGUMENTS
