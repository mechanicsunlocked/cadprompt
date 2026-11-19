"""Where things are, on Linux, macOS and Windows.

FreeCAD started from a desktop icon (above all on macOS) does not get the
shell's PATH, so claude is also looked for where its own installer puts it.
"""

import os
import shutil
import sys

import FreeCAD as App

WIN = sys.platform.startswith("win")
HOME = os.path.expanduser("~")

PARAMS = "User parameter:BaseApp/Preferences/Mod/CADprompt"


def params():
    return App.ParamGet(PARAMS)


def _candidates(name):
    exe = [name + ".exe", name + ".cmd", name] if WIN else [name]
    dirs = [
        os.path.join(HOME, ".local", "bin"),          # claude's native installer
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
    """Absolute path of a program (claude), or None. A path set in the preferences wins."""
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


def bundle_roots():
    """The folder of a FreeCAD that brings its own Python and libraries, as a list.

    An AppImage, a macOS app and a Windows install do. Empty for a FreeCAD that
    is a part of the system (a Linux package, Homebrew, conda): there the
    folders of FreeCAD are the folders of every other program too.
    """
    home = os.path.normpath(App.getHomePath())
    if WIN:
        # the official build has its own python.exe next to FreeCAD.exe; a conda build does not
        return [home] if os.path.isfile(os.path.join(home, "bin", "python.exe")) else []
    if sys.platform == "darwin":
        folder = home
        while folder != os.path.dirname(folder):
            if folder.lower().endswith(".app"):
                return [folder]
            folder = os.path.dirname(folder)
        return []
    appdir = os.path.dirname(home)          # an AppImage, mounted or unpacked: <AppDir>/usr and <AppDir>/AppRun
    return [appdir] if os.path.isfile(os.path.join(appdir, "AppRun")) else []


def _plain(path):
    return os.path.normcase(os.path.normpath(path))


def child_environment(env, roots=None):
    """`env` (a dict) for a program that the addon starts, without what belongs to FreeCAD.

    The start script of a FreeCAD bundle sets variables that point into the
    bundle: PYTHONHOME, PYTHONPATH, LD_LIBRARY_PATH, SSL_CERT_FILE and more.
    They are right for FreeCAD and wrong for every other program: with
    FreeCAD's PYTHONHOME no other Python starts. Every value that points into
    the bundle is taken out; a list (PATH) only loses those of its entries.
    """
    roots = bundle_roots() if roots is None else roots
    inside = set()
    for root in roots:
        inside.add(_plain(root).rstrip(os.sep) + os.sep)
        try:                                 # also under its real name (a link; macOS: /var is /private/var)
            inside.add(_plain(os.path.realpath(root)).rstrip(os.sep) + os.sep)
        except (OSError, ValueError):
            pass
    inside = tuple(inside)
    if not inside:
        return dict(env)
    appimage = any(os.path.isfile(os.path.join(r, "AppRun")) for r in roots)
    out = {}
    for name, value in env.items():
        if appimage and name in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):
            continue                         # they describe FreeCAD's AppImage, not the program we start
        parts = value.split(os.pathsep)
        # the text of each entry is compared, the disk is not asked: an entry can be on a slow network drive
        kept = [p for p in parts if not (os.path.isabs(p) and (_plain(p) + os.sep).startswith(inside))]
        if len(kept) == len(parts):
            out[name] = value
        elif kept:
            out[name] = os.pathsep.join(kept)
    return out


def in_flatpak():
    return os.path.exists("/.flatpak-info")
