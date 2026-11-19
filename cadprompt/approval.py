"""The one place where the user approves a step of the AI before it runs.

Both ways into FreeCAD call request() in the main thread before a tool that
changes things runs: the built-in engine (local_engine.LocalRun._run_tool) and
the bridge for Claude Code (bridge._Host._run). The tools are those of
local_prompt.CHANGING: run_python and make_drawing.

The chat panel shows the step and waits for Run or Skip (panel.Panel.ask_approval).
"Run the AI's code without asking" is the user's own switch (parameter
AutoApprove, off in a new profile). With no panel to ask and the switch off,
nothing runs. No Qt in here.
"""

from . import paths

DECLINED = "The user did not approve this step."     # the tool result when request() says no
PARAM = "AutoApprove"
WARNING_TITLE = "Run the AI's code without asking?"
WARNING = ("The AI's code runs as you, on this computer. It can change or delete files. A mistake of "
           "the AI, or a text that it read in a file or on a web page, can make it do what you did not "
           "want.\n\n"
           "Use this only for models that you can restore. Each run is one Undo step, and a backup copy "
           "of the model file is made before the first change of a request.\n\n"
           "The code is still shown in the chat. The marker \"runs without asking\" in the panel turns "
           "this off again with one click.")
WARNING_YES, WARNING_NO = "Run without asking", "Keep asking"

_askers = []      # the open panels, the newest last


def auto():
    """True when the user turned "Run the AI's code without asking" on."""
    return paths.params().GetBool(PARAM, False)


def set_auto(on):
    paths.params().SetBool(PARAM, bool(on))


def shown(tool_name, args):
    """The text that the user sees and approves: for run_python the code, exactly as
    local_tools runs it; for make_drawing one line that says what will be made."""
    args = args if isinstance(args, dict) else {}
    if tool_name == "run_python":
        return str(args.get("code", ""))
    sheet, obj_name = str(args.get("sheet") or "A3"), str(args.get("obj_name") or "")
    try:
        from . import drawing
        return drawing.describe(sheet, obj_name)
    except Exception:            # TechDraw is not there: the tool itself will say so
        return "Make drawing: sheet %s, %s" % (sheet, "object " + obj_name if obj_name else "the main object")


def register(asker):
    """A panel that can ask: asker.ask_approval(tool_name, args, source) -> bool, asker.asks() -> bool,
    asker.withdraw()."""
    if asker not in _askers:
        _askers.append(asker)


def unregister(asker):
    if asker in _askers:
        _askers.remove(asker)


def _asker():
    """The panel whose request is running now, or None."""
    for asker in reversed(_askers):
        try:
            if asker.asks():
                return asker
        except RuntimeError:     # Qt deleted the panel already
            continue
    return None


def withdraw():
    """The AI does not wait for the answer any more (Claude Code gave the tool call up):
    the question that is open ends with a no, and the request goes on. Main thread."""
    asker = _asker()
    if asker is not None:
        asker.withdraw()


def request(tool_name, args, source):
    """True when this step may run now, exactly as it is given here.

    tool_name  "run_python" or "make_drawing"
    args       the arguments of the call, a dict ({"code": ...} or {"sheet": ..., "obj_name": ...})
    source     who asks: "local", "apikey", "account" or "custom" (backends.BACKENDS)

    Called in FreeCAD's main thread. On False nothing runs and the AI gets
    DECLINED as the result of the tool. The question lets Qt events through
    while it waits: the callers look again afterwards whether their request
    is still the one that runs.
    """
    asker = _asker()
    if asker is None:
        return auto()            # nobody to ask: only the user's own switch says yes
    return bool(asker.ask_approval(tool_name, args, source))
