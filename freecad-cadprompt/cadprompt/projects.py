"""Project folders: model/ refs/ drawings/, the same layout the Omarchy launcher uses."""

import datetime
import os
import re
import shutil

import FreeCAD as App

from .paths import projects_root, template_dir


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
        with open(os.path.join(project, ".cadprompt", "mode")) as f:
            return f.read().strip() or "build"
    except OSError:
        return "build"


def set_mode(project, mode):
    os.makedirs(os.path.join(project, ".cadprompt"), exist_ok=True)
    with open(os.path.join(project, ".cadprompt", "mode"), "w") as f:
        f.write(mode + "\n")


def get_session(project):
    try:
        with open(os.path.join(project, ".cadprompt", "session")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def set_session(project, sid):
    p = os.path.join(project, ".cadprompt", "session")
    if sid:
        with open(p, "w") as f:
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
    real = os.path.realpath(path)
    for doc in App.listDocuments().values():
        if os.path.realpath(doc.FileName or "") == real:
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
