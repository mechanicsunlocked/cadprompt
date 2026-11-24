"""An `os` as a module sees it on Windows, for a test on any system.

    seen = []
    local_tools.os = windows_os(seen)      # and put the real one back after the check

The path rules are the real ones of Windows (ntpath). realpath opens nothing:
it writes down the path that Windows would open. On Windows that open is a
network login when the path names another computer (\\\\host\\share\\x), so a
check must refuse such a name before realpath sees it.
"""

import ntpath
import types

PROJECT = "C:\\Users\\student\\proj"
# names that are not in the project, and that must never be opened to find that out
FOREIGN = ("\\\\evil.example\\share\\a.png", "//evil.example/share/a.png", "\\\\evil.example",
           "\\\\?\\UNC\\evil.example\\share\\a.png", "\\\\.\\UNC\\evil.example\\share\\a.png",
           "D:\\x.png", "d:/x.png", "D:x.png")


def windows_os(seen):
    names = ("join", "split", "splitdrive", "normcase", "normpath", "isabs", "relpath", "basename", "dirname")
    path = types.SimpleNamespace(**{n: getattr(ntpath, n) for n in names})

    def realpath(p, **more):
        seen.append(p)
        return ntpath.normpath(p)

    path.realpath = realpath
    return types.SimpleNamespace(path=path, sep="\\", altsep="/", pardir="..", curdir=".", name="nt")


def outside(seen, project=PROJECT):
    """The paths in `seen` that are not the project folder or in it."""
    root = ntpath.normcase(project)
    return [p for p in seen if ntpath.normcase(p) != root and not ntpath.normcase(p).startswith(root + "\\")]
