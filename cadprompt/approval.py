"""The one place where the user approves a step of the AI before it runs.

Both ways into FreeCAD call request() in the main thread before a tool that
changes things runs: the built-in engine (local_engine.LocalRun._run_tool) and
the bridge for Claude Code (bridge._Host._run). The tools are those of
local_prompt.CHANGING: run_python and make_drawing.
"""

DECLINED = "The user did not approve this step."     # the tool result when request() says no


def request(tool_name, args, source):
    """True when this step may run now, exactly as it is given here.

    tool_name  "run_python" or "make_drawing"
    args       the arguments of the call, a dict ({"code": ...} or {"sheet": ..., "obj_name": ...})
    source     who asks: "local", "apikey", "account" or "custom" (backends.BACKENDS)

    Called in FreeCAD's main thread. On False nothing runs and the AI gets
    DECLINED as the result of the tool.

    For now every step is approved. The dialog comes next and belongs here:
    show the code, wait for Run or Skip, and offer "run without asking" as a
    choice that is off until the user turns it on.
    """
    return True
