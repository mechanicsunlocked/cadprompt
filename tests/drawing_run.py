"""Runs the drawing tests in FreeCAD with a fresh settings folder and shows the result.

    python3 tests/drawing_run.py [folder for the results]

1. tests/test_drawing_headless.py in freecadcmd (rules, and a call without a window),
2. tests/test_drawing_gui.py in freecad with QT_QPA_PLATFORM=offscreen (pages, views, PDFs),
3. every PDF as a PNG next to it (pdftoppm), to look at.

FREECAD_USER_HOME points into the results folder, so the user's own FreeCAD
settings are never read or changed.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import run_all                                              # noqa: E402

# With the offscreen platform FreeCAD 1.1 locks up as soon as it shows a
# notification (the popup's raise() logs a warning, which wants to show a
# notification, which waits for itself), and a new settings folder brings up
# the first-start dialog, which waits for a click. Both are switched off here.
USER_CFG = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FCParameters>
  <FCParamGroup Name="Root">
    <FCParamGroup Name="BaseApp">
      <FCParamGroup Name="Preferences">
        <FCParamGroup Name="NotificationArea">
          <FCBool Name="NotificationAreaEnabled" Value="0"/>
          <FCBool Name="NonIntrusiveNotificationsEnabled" Value="0"/>
        </FCParamGroup>
        <FCParamGroup Name="Mod">
          <FCParamGroup Name="Start">
            <FCBool Name="FirstStart2024" Value="0"/>
            <FCBool Name="ShowOnStartup" Value="0"/>
          </FCParamGroup>
        </FCParamGroup>
      </FCParamGroup>
    </FCParamGroup>
  </FCParamGroup>
</FCParameters>
"""


def fresh_home(root, name):
    home = os.path.join(root, name)
    os.makedirs(home)
    with open(os.path.join(home, "user.cfg"), "w", encoding="utf-8") as f:
        f.write(USER_CFG)
    return home


def run(cmd, env, seconds=300):
    """(exit code, output). 124 when it ran out of time. No shell tool: the same on every system."""
    fd, out_path = tempfile.mkstemp(prefix="cadprompt-drawing-", suffix=".txt")
    os.close(fd)
    try:
        code, _ = run_all.run(cmd, env, out_path, seconds)
        with open(out_path, encoding="utf-8", errors="replace") as f:
            out = f.read()
    finally:
        os.remove(out_path)
    return (124 if code is None else code), out


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="cadprompt-drawing-")
    root = os.path.join(os.path.abspath(root), time.strftime("run-%Y%m%d-%H%M%S"))
    os.makedirs(root)
    ok = True

    print("== 1. headless: freecadcmd tests/test_drawing_headless.py")
    env = dict(os.environ, FREECAD_USER_HOME=fresh_home(root, "home-headless"))
    found = run_all.find_freecad()       # FREECAD, FREECADCMD, PATH, the usual install folders
    code, out = run((found["cmd"] or ["freecadcmd"]) + [os.path.join("tests", "test_drawing_headless.py")], env)
    for line in out.splitlines():
        if line.startswith(("test_", "Ran ", "OK", "FAILED", "FAIL", "ERROR", "Traceback", "  File", "Assertion", "---", "===")):
            print(line)
    print(f"exit code {code}\n")
    ok = ok and code == 0

    print("== 2. with the window: QT_QPA_PLATFORM=offscreen freecad tests/test_drawing_gui.py")
    report = os.path.join(root, "test_drawing_gui.txt")
    projects = os.path.join(root, "projects")
    env = dict(os.environ, FREECAD_USER_HOME=fresh_home(root, "home-gui"), DRAWING_REPORT=report, DRAWING_OUT=projects)
    if sys.platform != "darwin":         # macOS: FreeCAD cannot open a document without a display
        run_all.offscreen(env)
    code, out = run((found["gui"] or ["freecad"]) + [os.path.join("tests", "test_drawing_gui.py")], env)
    if os.path.isfile(report):
        with open(report, encoding="utf-8") as f:
            print(f.read().rstrip())
    else:
        print("no report was written; FreeCAD said:\n" + out[-3000:])
    print(f"exit code {code}\n")
    ok = ok and code == 0

    print("== 3. the drawings")
    pdfs = sorted(glob.glob(os.path.join(projects, "*", "drawings", "*.pdf")))
    tool = shutil.which("pdftoppm")
    for pdf in pdfs:
        line = f"{pdf}  ({os.path.getsize(pdf)} bytes)"
        if tool:
            subprocess.run([tool, "-r", "80", "-png", "-singlefile", pdf, pdf[:-4]], check=False)
            line += "  -> " + os.path.basename(pdf)[:-4] + ".png"
        print(line)
    if not tool:
        print("pdftoppm not found: no PNG pictures")

    print("\nALL DRAWING TESTS PASSED" if ok else "\nDRAWING TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
