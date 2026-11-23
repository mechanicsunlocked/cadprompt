# SPDX-License-Identifier: MIT
"""Copy one pinned version of the Weldment workbench into this addon.

    python tools/vendor_weldment.py            the commit in weldment/VENDORED again
    python tools/vendor_weldment.py <ref>      move to another commit, tag or branch
    python tools/vendor_weldment.py --from DIR a local checkout instead of GitHub

The Addon Manager installs CADprompt as one folder and runs no setup, so the
workbench comes along as files: the package weldment/ next to hoischen/ and its
icons in icons/. The package keeps its name: a saved model stores its welds as
weldment.feature.FilletWeld, and models made with the standalone workbench must
open here too, and the other way round.

Nothing here runs inside FreeCAD. Needs git.
"""

import os
import shutil
import subprocess
import sys
import tempfile

REPO = "https://github.com/mechanicsunlocked/freecad-weldment"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(HERE, "weldment")
ICONS = os.path.join(HERE, "icons")
STAMP = os.path.join(PACKAGE, "VENDORED")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def pinned():
    try:
        with open(STAMP, encoding="utf-8") as f:
            for line in f:
                if line.startswith("commit "):
                    return line.split()[1]
    except OSError:
        pass
    return None


def main(argv):
    source, ref = REPO, None
    args = list(argv)
    if args[:1] == ["--from"]:
        source, args = os.path.abspath(args[1]), args[2:]
    ref = args[0] if args else pinned()
    if not ref:
        sys.exit("no weldment/VENDORED yet: name the commit, tag or branch to take")

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "weldment")
        git("clone", "-q", source, src)
        git("-c", "advice.detachedHead=false", "checkout", "-q", ref, cwd=src)
        commit = git("rev-parse", "HEAD", cwd=src)
        date = git("show", "-s", "--format=%cs", "HEAD", cwd=src)

        shutil.rmtree(PACKAGE, ignore_errors=True)
        os.makedirs(PACKAGE)
        for name in sorted(os.listdir(os.path.join(src, "weldment"))):
            if name.endswith(".py"):
                shutil.copy2(os.path.join(src, "weldment", name), PACKAGE)
        shutil.copy2(os.path.join(src, "LICENSE"), os.path.join(PACKAGE, "LICENSE"))
        icons = []
        for name in sorted(os.listdir(os.path.join(src, "icons"))):
            if name.endswith(".svg"):
                shutil.copy2(os.path.join(src, "icons", name), ICONS)
                icons.append(name)
        with open(STAMP, "w", encoding="utf-8", newline="\n") as f:
            f.write("The Weldment workbench, copied by tools/vendor_weldment.py. Do not edit here:\n"
                    "change it in freecad-weldment and copy it again.\n\n"
                    f"repository {REPO}\ncommit {commit}\ndate {date}\n"
                    f"icons {' '.join(icons)}\n")
    print(f"weldment {commit[:12]} ({date}) -> {os.path.relpath(PACKAGE, HERE)}, {len(icons)} icons")


if __name__ == "__main__":
    main(sys.argv[1:])
