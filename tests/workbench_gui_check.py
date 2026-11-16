# SPDX-License-Identifier: MIT
"""The CADprompt workbench inside a running FreeCAD. Started by test_workbench.py.

FreeCAD itself has loaded the addon from the Mod folder of the throw-away
profile (FREECAD_USER_HOME), the way an Addon Manager install is loaded; this
script adds nothing to sys.path. WORKBENCH_CASE says what the profile holds:

    default     a new profile: ShowInTechDraw is not set
    optin       ShowInTechDraw was True before FreeCAD started
    notechdraw  as optin, and FreeCAD was started with --disable-addon TechDraw
    autoload    as default, but CADprompt is the workbench FreeCAD starts in

The report goes to <FREECAD_USER_HOME>/workbench_gui_check.log.
"""

import os
import sys
import traceback

# before anything else is imported: what the start of FreeCAD loaded from the addon
EARLY = sorted(m for m in sys.modules if m.split(".")[0] in ("cadprompt", "hoischen"))

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME = os.environ.get("FREECAD_USER_HOME") or ""
CASE = os.environ.get("WORKBENCH_CASE", "default")
LOG = os.path.join(HOME or ".", "workbench_gui_check.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []                 # exceptions inside Qt slots do not reach us any other way

HOISCHEN_MODULES = ["hoischen", "hoischen.arrange", "hoischen.commands", "hoischen.layout", "hoischen.preset"]


def say(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    say(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def same(a, b):
    """The same folder or file, however the system spells it (\\ or /, upper or lower case, a link)."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def main():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets

    # Without a display Qt warns "This plugin does not support raise()" when FreeCAD
    # shows a notification, and FreeCAD 1.1 locks up while it reports that warning.
    QtCore.QLoggingCategory.setFilterRules("default.warning=false")

    mw = Gui.getMainWindow()
    params = App.ParamGet("User parameter:BaseApp/Preferences/Mod/CADprompt")

    def pump():
        for _ in range(5):
            QtWidgets.QApplication.processEvents()

    def activate(name):
        Gui.activateWorkbench(name)
        pump()
        return type(Gui.activeWorkbench()).__name__ == name

    def toolbars():
        """Every toolbar of the main window: name -> (on screen, command names)."""
        return {tb.objectName(): (tb.isVisible(), [a.objectName() for a in tb.actions() if not a.isSeparator()])
                for tb in mw.findChildren(QtWidgets.QToolBar)}

    def menu_entries():
        """Every command in the menu bar as 'Menu/Submenu/Command_Name'."""
        out = []

        def walk(menu, path):
            for a in menu.actions():
                if a.menu():
                    walk(a.menu(), path + [a.text().replace("&", "")])
                elif not a.isSeparator():
                    out.append("/".join(path + [a.objectName()]))

        for a in mw.menuBar().actions():
            if a.menu():
                walk(a.menu(), [a.text().replace("&", "")])
        return out

    def docks():
        return mw.findChildren(QtWidgets.QDockWidget, "CADpromptDock")

    def dock_shown():
        return len(docks()) == 1 and docks()[0].isVisible()

    # ---- 1. what the start of FreeCAD did ----
    say(f"-- case '{CASE}': after the start of FreeCAD --")
    mod = os.path.join(HOME, "Mod", "CADprompt")
    import cadprompt
    from cadprompt import workbench as W
    OURS = (W.PANEL,) + W.DRAWING

    def ours_on_screen():
        """Toolbars on screen and menus that hold one of our commands."""
        bars = {n: [c for c in cmds if c in OURS] for n, (shown, cmds) in toolbars().items() if shown}
        return ({n: c for n, c in bars.items() if c},
                [e for e in menu_entries() if e.rsplit("/", 1)[-1] in OURS])

    def in_techdraw():
        """Our commands in TechDraw's own toolbars."""
        return {n: [c for c in cmds if c in OURS] for n, (_shown, cmds) in toolbars().items()
                if n.startswith("TechDraw") and any(c in OURS for c in cmds)}

    check("(setup) FreeCAD uses the throw-away profile and loaded the addon from its Mod folder",
          same(App.getUserAppDataDir(), HOME) and os.path.isfile(os.path.join(mod, "package.xml"))
          and same(os.path.dirname(cadprompt.__file__), os.path.join(mod, "cadprompt"))
          and not [d for d in sys.path if os.path.isabs(d) and same(d, REPO)],
          f"{App.getUserAppDataDir()} {cadprompt.__file__} {[d for d in sys.path if os.path.isabs(d) and same(d, REPO)]}")
    opt_in = CASE in ("optin", "notechdraw")
    if CASE == "autoload":
        check("FreeCAD started in the workbench, so the panel and hoischen are loaded; the engine and the drawing code are not",
              set(["cadprompt.panel", "cadprompt.workbench"] + HOISCHEN_MODULES) <= set(EARLY)
              and "cadprompt.local_engine" not in EARLY and "cadprompt.drawing" not in EARLY, str(EARLY))
    else:
        want_early = ["cadprompt", "cadprompt.workbench"] + (HOISCHEN_MODULES if opt_in else [])
        check("the start loaded the workbench module and, because the user asked for the TechDraw buttons, hoischen"
              if opt_in else "the start loaded only the small workbench module", EARLY == want_early, str(EARLY))
        check("the start loaded no panel, no engine and no TechDraw code of ours",
              not [m for m in EARLY if m.startswith("cadprompt.") and m != "cadprompt.workbench"]
              and "cadprompt.panel" not in sys.modules and "cadprompt.drawing" not in sys.modules, str(EARLY))
    from cadprompt import paths
    check("the workbench reads the same settings group as the rest of the addon", W.PARAMS == paths.PARAMS)

    meta = App.Metadata(os.path.join(mod, "package.xml"))
    benches = meta.Content.get("workbench", [])
    classname = benches[0].Classname if benches else ""
    listed = Gui.listWorkbenches()
    wb = listed.get(classname)
    check("the workbench is listed, under the class name that package.xml gives",
          len(benches) == 1 and classname == "CADpromptWorkbench" and wb is not None
          and type(wb).__name__ == classname, f"{classname!r} {sorted(listed)}")
    if wb is None:
        return
    check("it is called CADprompt, has a tooltip and the icon icons/cadprompt.svg",
          wb.MenuText == "CADprompt" and len(wb.ToolTip) > 20 and os.path.isfile(wb.Icon)
          and same(wb.Icon, os.path.join(mod, "icons", "cadprompt.svg")),
          f"{wb.MenuText!r} {wb.Icon!r}")
    registered = [c for c in OURS if Gui.Command.get(c) is not None]
    first = type(Gui.activeWorkbench()).__name__
    if CASE == "autoload":
        check("FreeCAD is in the CADprompt workbench, with its two toolbars and the chat dock, without a click",
              first == classname and dock_shown() and registered == list(OURS)
              and ours_on_screen()[0] == {"CADprompt": [W.PANEL], "Drawing": list(W.DRAWING)}, str(ours_on_screen()))
    else:
        check("the chat command is registered at start, the drawing commands "
              + ("too (TechDraw buttons asked for)" if opt_in else "are not yet"),
              registered == (list(OURS) if opt_in else [W.PANEL]), str(registered))
        check(f"before the workbench is opened ({first} is active): no dock, no button and no menu of ours",
              not docks() and ours_on_screen() == ({}, []) and first != classname, str(ours_on_screen()))
    check("the saved choice ShowInTechDraw is " + ("on" if opt_in else "off (the default)"),
          W.show_in_techdraw() == opt_in)
    has_techdraw = "TechDrawWorkbench" in listed
    check("(setup) TechDraw is " + ("switched off for this run" if CASE == "notechdraw" else "there"),
          has_techdraw == (CASE != "notechdraw"))

    # ---- 2. the opt-in from the profile, before our workbench was ever opened ----
    if CASE == "optin":
        say("\n-- ShowInTechDraw was on at start --")
        ok = activate("TechDrawWorkbench")
        bars = toolbars()
        check("TechDraw's Attributes toolbar ends with the four drawing buttons, each once",
              ok and bars.get(W.TECHDRAW_TOOLBAR, (False, []))[1][-4:] == list(W.DRAWING)
              and in_techdraw() == {W.TECHDRAW_TOOLBAR: list(W.DRAWING)}, str(in_techdraw()))
        check("no other toolbar and no menu got anything, and the chat did not open by itself",
              ours_on_screen() == ({W.TECHDRAW_TOOLBAR: list(W.DRAWING)}, []) and not docks(), str(ours_on_screen()))
        ok = activate("PartDesignWorkbench")
        check("in Part Design nothing of ours is on screen", ok and ours_on_screen() == ({}, []), str(ours_on_screen()))

    # ---- 3. our workbench ----
    say("\n-- the CADprompt workbench --")
    ok = activate(classname)
    pump()
    bars, menus = ours_on_screen()
    check("activating it shows the toolbar CADprompt with the chat button",
          ok and toolbars().get("CADprompt") == (True, [W.PANEL]), str(toolbars().get("CADprompt")))
    check("and the toolbar Drawing with the four Hoischen buttons in order",
          toolbars().get("Drawing") == (True, list(W.DRAWING)), str(toolbars().get("Drawing")))
    check("no other toolbar holds a command of ours",
          bars == {"CADprompt": [W.PANEL], "Drawing": list(W.DRAWING)}, str(bars))
    check("the menu CADprompt holds the chat command and the submenu Drawing",
          menus == ["CADprompt/" + W.PANEL] + ["CADprompt/Drawing/" + c for c in W.DRAWING], str(menus))
    check("all five commands are registered now, each with an icon file that exists",
          all(Gui.Command.get(c) is not None and os.path.isfile(Gui.Command.get(c).getInfo().get("pixmap", ""))
              for c in OURS))
    check("(hoischen was loaded at start)" if opt_in else "hoischen is loaded now",
          all(m in sys.modules for m in HOISCHEN_MODULES))
    from hoischen import commands as H
    check("the Drawing tools are the Hoischen commands: presets first, then Arrange, then the settings",
          sorted(W.DRAWING) == sorted(H.COMMANDS)
          and W.DRAWING == ("Hoischen_Preset05", "Hoischen_Preset07", "Hoischen_Arrange", "Hoischen_Settings"))
    from cadprompt import panel
    dock = docks()[0] if docks() else None
    check("activating it opened the chat dock with the panel in it",
          dock_shown() and isinstance(dock.widget(), panel.Panel) and panel._dock is dock)
    if dock is None:
        return
    check("(setup) the panel made no connection: it waits for a server address",
          [k for k, _ in dock.widget().entries] == ["setup"] and dock.widget().claude is None
          and not dock.widget()._running(), str(dock.widget().entries))
    panel.show()
    pump()
    check("panel.show() with the dock on screen: the same dock, still on screen", dock_shown() and panel._dock is dock)
    dock.hide()
    pump()
    hidden = not dock.isVisible()
    panel.show()
    pump()
    check("panel.show() brings a closed dock back, and no second one", hidden and dock_shown() and panel._dock is dock)
    panel.toggle()
    pump()
    hidden = not dock.isVisible()
    W.show_panel()
    pump()
    check("the command hides the dock, show_panel() of the workbench brings it back",
          hidden and dock_shown() and len(docks()) == 1)

    # ---- 4. other workbenches ----
    say("\n-- other workbenches --")
    ok = activate("PartDesignWorkbench")
    bars, menus = ours_on_screen()
    check("Part Design: the chat dock is still open", ok and dock_shown())
    check("Part Design: the File toolbar holds none of our commands",
          toolbars()["File"][0] and not [c for c in toolbars()["File"][1] if c in OURS], str(toolbars()["File"]))
    check("Part Design: no toolbar on screen and no menu (Tools included) holds one", (bars, menus) == ({}, []),
          f"{bars} {menus}")
    if has_techdraw:
        ok = activate("TechDrawWorkbench")
        bars, menus = ours_on_screen()
        names = [n for n, (shown, _c) in toolbars().items() if shown and n.startswith("TechDraw")]
        check("TechDraw: the chat dock is still open", ok and dock_shown())
        check("TechDraw: its toolbars are there, the Attributes toolbar too",
              len(names) >= 5 and W.TECHDRAW_TOOLBAR in names, str(names))
        if not opt_in:
            check("TechDraw: neither the File toolbar nor a TechDraw toolbar nor a menu holds one of our commands",
                  in_techdraw() == {} and (bars, menus) == ({}, []), f"{in_techdraw()} {bars} {menus}")
        else:
            check("TechDraw: the drawing buttons are in Attributes (asked for), nothing else of ours",
                  (bars, menus) == ({W.TECHDRAW_TOOLBAR: list(W.DRAWING)}, []), f"{bars} {menus}")

    # ---- 5. the dock belongs to the user ----
    say("\n-- closing and reopening the chat --")
    activate("PartDesignWorkbench")
    dock.close()
    pump()
    closed = not dock.isVisible()
    activate("PartWorkbench")
    check("a closed chat stays closed in other workbenches", closed and not dock.isVisible())
    activate(classname)
    check("going back to the CADprompt workbench opens it again", dock_shown())
    activate("PartDesignWorkbench")
    activate(classname)
    check("activating the workbench with the chat open leaves it open (no toggle), and there is one dock",
          dock_shown())
    Gui.runCommand(W.PANEL)
    pump()
    hidden = not dock.isVisible()
    Gui.runCommand(W.PANEL)
    pump()
    check("the chat button hides and shows the same dock", hidden and dock_shown())
    bars, _menus = ours_on_screen()
    check("the workbench still has its two toolbars, nothing twice",
          bars == {"CADprompt": [W.PANEL], "Drawing": list(W.DRAWING)}, str(bars))

    # ---- 6. set_show_in_techdraw ----
    say("\n-- set_show_in_techdraw --")
    if not has_techdraw:
        try:
            W.set_show_in_techdraw(False)
            W.set_show_in_techdraw(True)
            ok = W.show_in_techdraw() and ours_on_screen()[0] == {"CADprompt": [W.PANEL], "Drawing": list(W.DRAWING)}
            W.set_show_in_techdraw(False)
            detail = ""
        except Exception:
            ok, detail = False, traceback.format_exc()
        check("without TechDraw: the choice can be changed, nothing breaks and nothing moves", ok, detail)
    else:
        activate("TechDrawWorkbench")
        W.set_show_in_techdraw(False)
        pump()
        check("off while TechDraw is on screen: the buttons are gone at once and the choice is saved",
              in_techdraw() == {} and ours_on_screen() == ({}, []) and params.GetBool("ShowInTechDraw", True) is False,
              str(in_techdraw()))
        W.set_show_in_techdraw(True)
        pump()
        check("on while TechDraw is on screen: the buttons are there at once and the choice is saved",
              in_techdraw() == {W.TECHDRAW_TOOLBAR: list(W.DRAWING)} and params.GetBool("ShowInTechDraw", False) is True,
              str(in_techdraw()))
        W.set_show_in_techdraw(True)
        pump()
        activate("PartDesignWorkbench")
        check("on: still nothing in Part Design", ours_on_screen() == ({}, []), str(ours_on_screen()))
        activate("TechDrawWorkbench")
        check("on, asked twice, TechDraw opened again: each button once",
              in_techdraw() == {W.TECHDRAW_TOOLBAR: list(W.DRAWING)}, str(in_techdraw()))
        activate("PartDesignWorkbench")
        W.set_show_in_techdraw(False)
        activate("TechDrawWorkbench")
        check("off from another workbench: gone the next time TechDraw is opened",
              in_techdraw() == {} and ours_on_screen() == ({}, []), str(in_techdraw()))
        activate("PartDesignWorkbench")
        W.set_show_in_techdraw(True)
        activate("TechDrawWorkbench")
        check("on from another workbench: there the next time TechDraw is opened",
              in_techdraw() == {W.TECHDRAW_TOOLBAR: list(W.DRAWING)}, str(in_techdraw()))
        W.set_show_in_techdraw(False)
        pump()
    check("the chat dock lived through all of it", dock_shown())
    check("no error came up inside a Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))


def _hook(kind, value, tb):
    SLOT_ERRORS.append("".join(traceback.format_exception(kind, value, tb)))


if not HOME:
    os._exit(2)      # this test must not touch the user's FreeCAD settings
open(LOG, "w", encoding="utf-8").close()
sys.excepthook = _hook
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
