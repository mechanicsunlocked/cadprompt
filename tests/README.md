# Tests

One command runs all tests. It works the same on Linux, macOS and Windows:

    python3 tests/run_all.py

On Windows the command is `python tests\run_all.py`.

You need Python 3 and FreeCAD 1.0 or newer. You do not need a model server, an
API key or the `claude` program. Nothing is installed. On Linux and Windows you
do not need a display. On macOS the window tests show their windows on your
screen: FreeCAD on macOS cannot open a document without a display (see below).

**The rule for all tests: a test never touches your own FreeCAD settings.**
`run_all.py` makes a new, empty settings folder for each test and gives it to
FreeCAD in the variable `FREECAD_USER_HOME`. Your FreeCAD profile, your macros
and your addons are not read and not changed. Do not start a test file with
your FreeCAD by hand without this variable.

## What you see

    FreeCAD 1.1.4 (45040 (Git)), Python 3.14.8
      freecadcmd  /usr/bin/freecadcmd
      freecad     /usr/bin/freecad

    PASS       0.1 s  python       hoischen/test_layout
    PASS       1.2 s  freecadcmd   test_backends
    FAIL      10.5 s  freecad-gui  test_local_engine  (exit code 1)
            | -- test_local_engine.log
            | FAIL  finished once with stopped=True ...
    ...
    12 tests in 45 s: 11 passed, 1 failed, 0 timed out, 0 skipped

- The first line is the FreeCAD version. When a new FreeCAD breaks the addon,
  this line says which one.
- There is one line for each test: `PASS`, `FAIL`, `TIMEOUT` or `SKIP`, the
  time, how the test is started, and its name.
- For a failed test you see the last lines of its log.
- The exit code is 0 only when no test failed and no test ran out of time.
- The folders of failed tests stay on the disk. The last line says where.
  Each folder has `output.txt` (what the program printed) and `home/` (the
  FreeCAD settings folder of that test, with the test's own log and files).

## Options

| Option | What it does |
|---|---|
| `-k TEXT` | Runs only the tests with TEXT in their name. You can give it more than once. |
| `--list` | Shows the tests and how each one is started. Runs nothing. |
| `--no-gui` | Skips the tests that need the FreeCAD window. |
| `--gui-only` | Runs only the tests that need the FreeCAD window. |
| `--display` | Shows the FreeCAD window on your screen. Without this option no window appears (macOS: always on). |
| `--timeout S` | Stops a test after S seconds (default 300). The test and all programs it started are stopped. |
| `--keep` | Keeps the temporary folders of all tests, not only of the failed ones. |
| `--out FOLDER` | Puts the test folders into FOLDER (in a new `run-<date>-<time>` folder) and keeps them. |
| `--junit FILE` | Also writes the results as a JUnit XML file. |
| `--summary FILE` | Also adds the results to FILE as a Markdown table. `--title TEXT` sets its heading. |

## How run_all.py finds FreeCAD

It looks in this order and uses the first FreeCAD that it finds:

1. the variables `FREECAD` (the program with the window) and `FREECADCMD`
   (the program without a window). `FREECAD` can also be an install folder,
   a macOS `FreeCAD.app` or an AppImage. If you set only one of the two, the
   other program is taken from the same install.
2. `freecad`, `FreeCAD`, `freecadcmd`, `FreeCADCmd` in `PATH`,
3. the usual install folders: `Program Files\FreeCAD*\bin` on Windows,
   `/Applications/FreeCAD.app` on macOS.

If `FREECAD` or `FREECADCMD` names something that does not exist, the FreeCAD
tests fail with that message. If no FreeCAD is found, the FreeCAD tests fail
with "not found". Tests are not skipped silently.

### Linux

With FreeCAD from your distribution, `python3 tests/run_all.py` is enough.

With an AppImage:

    FREECAD=~/Downloads/FreeCAD_1.1.4-Linux-x86_64-py311.AppImage python3 tests/run_all.py

`run_all.py` then starts `<AppImage> freecadcmd <script>` and
`<AppImage> freecad <script>`. The start script in the AppImage runs the
program in its `usr/bin` that the first word names.

Two things to know about the AppImage:

- The AppImage of FreeCAD 1.1 always uses the X display. It ignores the
  setting for "no display". `run_all.py` tells you when this is the case. The
  window tests then need a display: your screen, or a virtual one
  (`xvfb-run -a python3 tests/run_all.py`). Later versions read the variable
  `FREECAD_QT_QPA_PLATFORM`, which `run_all.py` sets, and need no display.
- An AppImage needs FUSE to start. Without FUSE, unpack it one time with
  `./FreeCAD_….AppImage --appimage-extract` and use
  `FREECAD=<folder>/squashfs-root/AppRun`.

### macOS

With FreeCAD in `/Applications`, `python3 tests/run_all.py` is enough.
The window tests show the FreeCAD window on the screen for some seconds each.
The reason: Qt's "offscreen" platform has no OpenGL on macOS, and FreeCAD 1.1.4
ends with a segmentation fault when a document gets its 3D view there.
For a FreeCAD in a different place:

    FREECAD=~/Downloads/FreeCAD.app python3 tests/run_all.py

### Windows

With FreeCAD installed in `Program Files`, `python tests\run_all.py` is enough.
For the unpacked `.7z` archive, in PowerShell:

    $env:FREECAD = "C:\Tools\FreeCAD_1.1.4-Windows-x86_64-py311"
    python tests\run_all.py

## What each test needs

`python3 tests/run_all.py --list` shows the current list. Today:

| Test | Started with | What it needs and what it tests |
|---|---|---|
| `hoischen/test_layout` | python | Only Python. The rules for dimension lines. |
| `test_local_agent` | python | Only Python. The agent loop against a scripted server. |
| `test_time_limit` | python | Only Python. The time limit that stops code which does not end. |
| `test_workbench` | python | Starts `freecadcmd` and `freecad` itself (found the same way as above). FreeCAD loads a copy of the addon from a new profile. |
| `test_backends` | freecadcmd | The AI sources and the environment for Claude Code. Every key is made up. |
| `test_drawing_headless` | freecadcmd | The drawing rules, and a drawing call without a window. |
| `test_local_tools` | freecadcmd | The four tools of the built-in engine. |
| `hoischen/gui_check` | freecad-gui | The DIN dimension layout in a real TechDraw page. FreeCAD loads the addon (`--module-path`), with the setting `ShowInTechDraw` on. |
| `test_drawing_gui` | freecad-gui | Drawings with pages, views and PDF files. |
| `test_integration_gui` | freecad-gui | The whole addon. A scripted server plays the model. |
| `test_local_engine` | freecad-gui | The built-in engine. A scripted server plays the model. |
| `test_panel` | freecad-gui | The panel and the settings dialog. A pretend `claude` and a stub engine. |
| `test_panel_engine` | freecad-gui | The panel with the real engine. A scripted server plays the model. |

Not run by `run_all.py`:

- `local_smoke.py` and the runs that you switch on with `INTEGRATION_REAL_MODEL`
  or `PANEL_REAL_MODEL`. They need a real model server. `run_all.py` removes
  these variables (all names with `_REAL_`, and `CADPROMPT_SMOKE_*`) before it
  starts a test, so it never calls a real model. It also removes
  `ANTHROPIC_*` and `OPENAI_API_KEY`, so no key of your computer gets into a test.
- `drawing_run.py`. It runs the two drawing tests and makes PNG pictures of the
  drawings to look at. Use it when you work on drawings.
- The other files are helpers for the tests (fake server, pretend `claude`,
  test parts).

## How to add a test

1. Name the file `tests/test_<something>.py` (a sub-folder of `tests/` is fine).
2. Write one of these comment lines in the first 30 lines of the file:

       # RUN: python          plain Python, no FreeCAD
       # RUN: freecadcmd      FreeCAD without a window
       # RUN: freecad-gui     FreeCAD with its window (no display on Linux and Windows)
       # RUN: manual          needs a real model or the real claude: run_all.py never runs it

   Without this line `run_all.py` guesses from the imports and tells you so.
3. The exit code is the result: 0 for pass, a different number for fail.
   In FreeCAD, end the script with `os._exit(code)`. FreeCAD does not stop by
   itself when a script ends or raises an error.
4. Read `FREECAD_USER_HOME` and put all files of the test there. A window test
   also writes its report to `<FREECAD_USER_HOME>/<name of the test>.log`,
   because the output of the FreeCAD window is not reliable. `run_all.py` shows
   the last lines of this file when the test fails.
5. A plain Python test that starts FreeCAD itself asks `run_all.find_freecad()`
   for the programs and starts them with `run_all.run()`. Do not call shell
   tools such as `timeout`: they are different programs on Windows.
6. Compare folders with `os.path.realpath` and `os.path.normcase`, not as text.
   Windows mixes `\` and `/` and upper and lower case, and the temporary folder
   of macOS has two names. Call `utf8_output()` from `utf8_output.py` first and
   give every `open()` of a text file `encoding="utf-8"`.
7. Use a scripted server (`local_fake_server.py`) in place of a model. A run
   with a real model is switched on with a variable that has `_REAL_` in its
   name, and is off by default.

## What run_all.py does for each test

- It makes a new folder with `home/` and sets `FREECAD_USER_HOME` to it.
- It writes `home/user.cfg` with the first-start dialog off and the
  notification area off. Without a display, FreeCAD 1.1 stops at the first one
  and locks up at the second one.
- It sets `QT_QPA_PLATFORM=offscreen` (and `FREECAD_QT_QPA_PLATFORM=offscreen`
  for the AppImage), so no window appears. `--display` switches this off, and
  so does macOS.
- It sets `PYTHONIOENCODING=utf-8` and `PYTHONFAULTHANDLER=1` (a crash then
  shows the Python line), and `CADPROMPT_TEST_PYTHON` to the Python that runs
  `run_all.py`, for a test that needs a plain Python next to FreeCAD.
- It starts the test from the repository folder: `python <file>`,
  `freecadcmd <file>` or `freecad <file>`.
- For `freecadcmd` it adds a second small script after the test. If the test
  stops before it gives a result (an error while FreeCAD loads it, or no
  `os._exit`), this script ends FreeCAD with exit code 97 and the test fails.
  Without it `freecadcmd` would end with 0 and the broken test would look like
  a pass.
- For the window it watches the output. If FreeCAD cannot load the test script,
  the test fails after some seconds and does not wait for the timeout.
- After the timeout it stops the test and every program that the test started.

`SPECIAL` in `run_all.py` holds what `hoischen/gui_check.py` needs in
addition: the addon loaded by FreeCAD, the setting `ShowInTechDraw` on (its
drawing buttons in TechDraw), `HOISCHEN_EXIT=1` and the place for its report.

## The tests on GitHub

`.github/workflows/tests.yml` runs `run_all.py` on each push and pull request,
on request, and one time each week. It downloads FreeCAD from the official
FreeCAD releases and finds the file names at run time, so no version number is
written in the file.

| Job | FreeCAD | Tests |
|---|---|---|
| Linux, FreeCAD stable | newest release, AppImage | all |
| Linux, FreeCAD weekly development build | newest weekly build, AppImage | all. This job is allowed to fail: it shows early what the next FreeCAD breaks. |
| Windows, FreeCAD stable | newest release, `.7z` archive | all. The window tests are allowed to fail until they are proven there. |
| macOS, FreeCAD stable | newest release, `.dmg` | all. The window tests are allowed to fail until they are proven there. |

When a job fails, its test folders (logs, reports, settings folders) are kept
as an artifact with the name `test-logs-<job>` for 14 days.
