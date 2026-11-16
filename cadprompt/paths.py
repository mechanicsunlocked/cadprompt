"""Where things are, on Linux, macOS and Windows.

FreeCAD started from a desktop icon (above all on macOS) does not get the
shell's PATH, so claude and uvx are also looked for where their own
installers put them.
"""

import os
import shlex
import shutil
import sys

import FreeCAD as App

FREECAD_MCP_VERSION = "0.1.28"  # addon and MCP server must match (lib/common.sh pins the same)
WIN = sys.platform.startswith("win")
HOME = os.path.expanduser("~")

PARAMS = "User parameter:BaseApp/Preferences/Mod/CADprompt"


def params():
    return App.ParamGet(PARAMS)


def _candidates(name):
    exe = [name + ".exe", name + ".cmd", name] if WIN else [name]
    dirs = [
        os.path.join(HOME, ".local", "bin"),          # claude and uv native installers
        os.path.join(HOME, ".cargo", "bin"),          # older uv installs
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
        os.path.join(HOME, ".local", "share", "mise", "shims"),
    ]
    if WIN:
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        dirs += [os.path.join(appdata, "npm"), os.path.join(local, "Programs", "claude"),
                 os.path.join(local, "Microsoft", "WinGet", "Links")]
    for d in dirs:
        for e in exe:
            yield os.path.join(d, e)


def find_tool(name):
    """Absolute path of claude / uvx, or None. A path set in the preferences wins."""
    custom = params().GetString(name + "Path", "")
    if custom and os.path.isfile(custom):
        return custom
    found = shutil.which(name)
    if found:
        return found
    for c in _candidates(name):
        if os.path.isfile(c):
            return c
    return None


def freecadcmd_string():
    """Headless FreeCAD as one shlex-quoted string (freecad-mcp's --freecadcmd).

    Forward slashes on Windows, because freecad-mcp splits it POSIX-style.
    """
    bindir = os.path.join(App.getHomePath(), "bin")
    for n in ("FreeCADCmd.exe", "freecadcmd.exe", "FreeCADCmd", "freecadcmd"):
        p = os.path.join(bindir, n)
        if os.path.isfile(p):
            return shlex.quote(os.path.normpath(p).replace("\\", "/"))
    return shutil.which("freecadcmd") or "FreeCADCmd"


def projects_root():
    custom = params().GetString("ProjectsDir", "")
    if custom:
        return custom
    omarchy = os.path.join(HOME, "Work", "CAD")   # shared with the Omarchy launcher
    if os.path.isdir(omarchy):
        return omarchy
    docs = os.path.join(HOME, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else HOME, "CAD")


def template_dir():
    """The repo's template/, found from this file (real path, so symlinks work)."""
    here = os.path.dirname(os.path.realpath(__file__))
    for up in range(1, 5):
        d = os.path.normpath(os.path.join(here, *[".."] * up, "template"))
        if os.path.isfile(os.path.join(d, "CLAUDE.md")):
            return d
    return None


def in_flatpak():
    return os.path.exists("/.flatpak-info")
