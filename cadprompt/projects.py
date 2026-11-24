"""Project folders: model/ refs/ drawings/, the same layout the Omarchy launcher uses."""

import datetime
import filecmp
import hashlib
import os
import re
import shutil
import time

import FreeCAD as App

from .paths import projects_root, template_dir


BACKUPS = 10      # copies kept of each model in model/.backup

# Projects are copies of template/, and the template changes with the addon. For each file
# that changed: the SHA-256 of every earlier version (line ends as LF). A project file that
# is one of these was never changed by the user, so the new version can take its place.
OLD = {
    ".mcp.json": ("6063d993d17936e86554387b2560345a050f21a6991c99a9a02c30a15727de37",
                  "5ff716b3a1230aef643bad022fe38bc573f259906283d5feeb04a5d0eb9ad88e"),
    ".claude/settings.json": ("d3560e37dc1c09c85651280b7f99fc0d2495d8099d67d26d07f616ffeea27d8a",
                              "a31d13aa5ba48ed15399043c7d22f35d11ac8474649037519f5098fe805592c3"),
    ".claude/commands/build.md": ("0fce8d988123bb0985d324be50108406971d07e62d7ed6ded8851411de347482",),
    ".claude/commands/teach.md": ("0dcc70e27f67736f2f41b03ccc3519fd33f7c02e3adf0ad2240b3d62bf41e9ae",),
    ".claude/commands/check.md": ("30c6d72929571ce50254aeaf724c4663621613291de25cc7d398c4d5035538e9",
                                  "fafa97cf9ef6e908df832ddf6fa7e73f8cfa261f0a06eff6315c23a3072efbc6"),
    ".claude/commands/drawing.md": ("c2ed1b666962393bb6cdc796370c6c9f49a2508d110aad2a2ffaef45f59ea83a",
                                    "beb50d4d7805f7832553ef1e1c5cfe5d3f08c5ba0d92f94cb9cc7141dd07b5ce"),
    "CLAUDE.md": ("16c69921f8375c6c42958a3adf148432d7ceb8b6accb1f85b945c7a2a43a4817",
                  "a336ff37d32f4d322d4c25faec973beacbf2bfacb62d3e6e2e806bc86dd6ef56",    # 0.2.1, before welding
                  "8e993b79993f1485a733a203d8ada1558f845d142db507d283b3ce105f6d91ee"),   # 0.3.0, before "save a file with run_python"
}
NEEDED = ("CLAUDE.md",)       # Claude Code cannot work in a project without it
# tools of the bridge that CADprompt used before it had its own
OLD_TOOLS = ("execute_code", "list_documents", "get_object", "get_objects", "create_object", "edit_object",
             "delete_object", "create_document", "insert_part_from_library", "get_parts_list")
NEW_TOOLS = "`run_python`, `get_document_state`, `make_drawing`, `find_joints`, `make_weld` and `get_view`"


def slug(name):
    s = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
    return s or "untitled"


def list_projects():
    """Project names, most recently used first."""
    root = projects_root()
    if not os.path.isdir(root):
        return []
    dirs = [d for d in os.listdir(root)
            if not d.startswith(".") and os.path.isdir(os.path.join(root, d))]
    return sorted(dirs, key=lambda d: os.path.getmtime(os.path.join(root, d)), reverse=True)


def create(name):
    tpl = template_dir()
    if not tpl:
        raise RuntimeError("template/ not found next to the addon")
    path = os.path.join(projects_root(), slug(name))
    if os.path.exists(path):
        return path
    shutil.copytree(tpl, path)
    return path


def path_of(name):
    return os.path.join(projects_root(), name)


def get_mode(project):
    try:
        with open(os.path.join(project, ".cadprompt", "mode"), encoding="utf-8") as f:
            return f.read().strip() or "build"
    except (OSError, ValueError):
        return "build"


def set_mode(project, mode):
    os.makedirs(os.path.join(project, ".cadprompt"), exist_ok=True)
    with open(os.path.join(project, ".cadprompt", "mode"), "w", encoding="utf-8") as f:
        f.write(mode + "\n")


def get_session(project):
    try:
        with open(os.path.join(project, ".cadprompt", "session"), encoding="utf-8") as f:
            return f.read().strip() or None
    except (OSError, ValueError):
        return None


def set_session(project, sid):
    p = os.path.join(project, ".cadprompt", "session")
    if sid:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(sid + "\n")
    elif os.path.exists(p):
        os.remove(p)


def newest_model(project):
    d = os.path.join(project, "model")
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".fcstd")]
    except OSError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def open_model(path):
    """Open a model in this FreeCAD unless it is open already."""
    def same(p):                 # Windows: FreeCAD writes C:/..., and upper and lower case are one file
        return os.path.normcase(os.path.realpath(p))

    real = same(path)
    for doc in App.listDocuments().values():
        if doc.FileName and same(doc.FileName) == real:
            return doc
    return App.openDocument(path)


def add_refs(project, files):
    """Copy files into refs/ and note them in refs/INDEX.md. Returns the new names."""
    refs = os.path.join(project, "refs")
    os.makedirs(refs, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    added = []
    with open(os.path.join(refs, "INDEX.md"), "a", encoding="utf-8") as index:
        for src in files:
            base = f"{stamp}-{os.path.basename(src)}"
            dest, n = os.path.join(refs, base), 1
            while os.path.exists(dest):
                root, ext = os.path.splitext(base)
                dest, n = os.path.join(refs, f"{root}-{n}{ext}"), n + 1
            shutil.copy2(src, dest)
            when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            index.write(f"- {when}  `{os.path.basename(dest)}`  (added in FreeCAD)\n")
            added.append(os.path.basename(dest))
    return added


def add_link(project, url):
    refs = os.path.join(project, "refs")
    os.makedirs(refs, exist_ok=True)
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(refs, "links.md"), "a", encoding="utf-8") as f:
        f.write(f"- {when}  <{url}>\n")
    with open(os.path.join(refs, "INDEX.md"), "a", encoding="utf-8") as f:
        f.write(f"- {when}  links.md  (link: {url})\n")


def backup(project, path):
    """Copy a model file to <project>/model/.backup/<name>-YYYYmmdd-HHMMSS.FCStd.

    The newest BACKUPS copies of each model are kept. Returns the path of the
    copy, or None when the file was not saved since the newest copy.
    """
    if not os.path.isfile(path):
        return None
    folder = os.path.join(project, "model", ".backup")
    name = os.path.splitext(os.path.basename(path))[0]
    ours = re.compile(re.escape(name) + r"-\d{8}-\d{6}\.FCStd")
    os.makedirs(folder, exist_ok=True)
    copies = sorted(f for f in os.listdir(folder) if ours.fullmatch(f))    # the names sort by time
    if copies and filecmp.cmp(path, os.path.join(folder, copies[-1]), shallow=False):
        return None
    copy = "%s-%s.FCStd" % (name, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, os.path.join(folder, copy))
    for old in sorted(set(copies + [copy]))[:-BACKUPS]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return os.path.join(folder, copy)


# ---- a project made from an older template ---------------------------------------
def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _digest(data):
    """Git on Windows can write the template with CRLF line ends, and a project copied from
    it has them too: the same text must give the same number on every system."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _free_name(path, end):
    """A name beside a file that nothing has yet: x.old, then x.old-2, x.old-3, ..."""
    name, n = path + end, 2
    while os.path.lexists(name):
        name, n = "%s%s-%d" % (path, end, n), n + 1
    return name


def _copy(src, dest, made):
    """Copy to a name that must be free. "x" refuses a file or a link that is there: a link
    can lead out of the project, and the copy would go into the file at its end."""
    with open(src, "rb") as f, open(dest, "xb") as out:
        made.append(dest)
        shutil.copyfileobj(f, out)


def _in_project(project, rel):
    """Is the folder of this file a real folder of the project? `.claude` can be a link (on
    Windows a junction) to a folder somewhere else, and that folder is not ours to change."""
    folder = os.path.join(*rel.split("/")[:-1]) if "/" in rel else ""
    here = os.path.realpath(project)
    return (os.path.normcase(os.path.realpath(os.path.join(project, folder)))
            == os.path.normcase(os.path.join(here, folder) if folder else here))


def _put(new, path, keep=True):
    """Put the template file `new` at `path`. The file that was there stays as <name>.old."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kept = _free_name(path, ".old") if keep and os.path.lexists(path) else None
    part = _free_name(path, ".new")      # never a name that is there: it can be the user's file, or a link
    made = []
    try:
        if kept:
            _copy(path, kept, made)
        _copy(new, part, made)
        os.replace(part, path)
    except OSError:
        # Windows: a file that another program has open cannot be replaced. Leave no half work.
        for name in made:
            try:
                os.remove(name)
            except OSError:
                pass
        raise


def migrate(project, replaced=None):
    """Bring the files that a project got from an older template up to date.

    Only a file that is still exactly an earlier version of the template is
    replaced, and the old one is kept beside it as <name>.old. A file that
    the user changed is never touched, and nothing is written through a link
    that leads out of the project. The names of the files that were replaced
    are added to the list `replaced`. Returns notes for the user: what a file
    that was left alone means for the work with Claude Code.

    The .mcp.json of a project gets no note: the panel gives Claude Code the
    one of the addon (runner.mcp_config).
    """
    tpl = template_dir()
    if not tpl or not os.path.isdir(project):
        return []
    for rel, old in OLD.items():
        new = os.path.join(tpl, *rel.split("/"))
        path = os.path.join(project, *rel.split("/"))
        if not os.path.isfile(new) or not _in_project(project, rel):
            continue
        data = _read(path)
        try:
            if data is None:
                if rel in NEEDED and not os.path.lexists(path):
                    _put(new, path, keep=False)
            elif _digest(data) in old:
                _put(new, path)
                if replaced is not None:
                    replaced.append(rel)
        except OSError as exc:
            App.Console.PrintWarning(f"CADprompt: could not bring {rel} of the project up to date: {exc}\n")
    notes = []
    text = (_read(os.path.join(project, "CLAUDE.md")) or b"").decode("utf-8", "replace")
    named = [t for t in OLD_TOOLS if re.search(r"\b%s\b" % t, text)]
    if named:
        notes.append("This project has its own `CLAUDE.md`, so CADprompt did not replace it. It names old "
                     "tools (%s). The tools are now %s. Correct the file, or delete it and send your "
                     "message again to get the new one." % (", ".join("`%s`" % t for t in named[:3]), NEW_TOOLS))
    return notes
