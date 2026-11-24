# RUN: freecadcmd
"""Tests for projects.migrate(): a project made from an older template gets the new files.

Run headless, with a throw-away FreeCAD profile:

    FREECAD_USER_HOME=$(mktemp -d) timeout 300 freecadcmd tests/test_migrate.py

old_template.json holds the template files as they were before. A file that
the user changed must come through every check here byte for byte.
"""

import json
import os
import shutil
import sys
import traceback

if not os.environ.get("FREECAD_USER_HOME"):
    sys.stdout.write("Set FREECAD_USER_HOME to a fresh folder first: this test must not touch your FreeCAD settings.\n")
    sys.stdout.flush()
    os._exit(2)

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

FAILED = []
COUNT = [0]


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    print(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def main():
    from cadprompt import paths, projects, runner

    home = os.environ["FREECAD_USER_HOME"]
    paths.params().SetString("ProjectsDir", os.path.join(home, "projects"))
    tpl = paths.template_dir()
    with open(os.path.join(HERE, "old_template.json"), encoding="utf-8") as f:
        old = json.load(f)
    before, older = old["before"], old["older"]
    changed = sorted(r for r in projects.OLD if projects.OLD[r])   # the files that have a new version
    # files that are new in the template (no old version) are made when a project has none
    same = sorted(set(before) - set(changed))            # the files that are as they were

    def new(rel):
        return read(os.path.join(tpl, *rel.split("/")))

    def make(name, files=None, crlf=False):
        """A project as an older CADprompt made it: the old files, a model and a reference."""
        project = os.path.join(home, "projects", name)
        for rel, text in dict(before, **(files or {})).items():
            path = os.path.join(project, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = text.encode("utf-8")
            with open(path, "wb") as f:
                f.write(data.replace(b"\n", b"\r\n") if crlf else data)
        for rel, data in (("model/part.FCStd", b"not a real model"), ("refs/INDEX.md", b"# References\n"),
                          (".cadprompt/mode", b"teach\n"), (".cadprompt/session", b"session-1\n")):
            path = os.path.join(project, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        return project

    def files(project):
        """{relative path: bytes} of everything in the project."""
        out = {}
        for folder, _subs, names in os.walk(project):
            for n in names:
                path = os.path.join(folder, n)
                out[os.path.relpath(path, project).replace(os.sep, "/")] = read(path)
        return out

    # ---- the test data and the table in projects.py fit together ----
    check("(setup) the old template is the one with uvx, the new one points at the bridge",
          '"command": "uvx"' in before[".mcp.json"] and b"CADPROMPT_MCP_URL" in new(".mcp.json")
          and b"uvx" not in new(".mcp.json"))
    check("every old version in the test data is known to migrate()",
          all(projects._digest(before[rel].encode("utf-8")) in projects.OLD[rel] for rel in changed)
          and all(projects._digest(text.encode("utf-8")) in projects.OLD[rel] for rel, text in older.items()),
          str(changed))
    check("no file of the template of today counts as old",
          not [rel for rel in changed if projects._digest(new(rel)) in projects.OLD[rel]])
    check("the files that changed: .mcp.json, CLAUDE.md, four commands, and settings.json",
          changed == [".claude/commands/build.md", ".claude/commands/check.md", ".claude/commands/drawing.md",
                      ".claude/commands/teach.md", ".claude/settings.json", ".mcp.json", "CLAUDE.md"], str(changed))
    perms = json.loads(new(".claude/settings.json").decode("utf-8"))["permissions"]
    check("the settings of today switch no question of Claude Code off: no mode, no allow list; no shell",
          "defaultMode" not in perms and "allow" not in perms and "Bash" in perms["deny"], str(perms))
    check("... and the two mode commands do not tell Claude Code to write the mode file first",
          not [rel for rel in (".claude/commands/build.md", ".claude/commands/teach.md")
               if new(rel).split(b"---")[2].lstrip().startswith(b"Write")])
    check("the new template names the new tools and none of the old ones",
          all(t.encode() in new("CLAUDE.md") for t in ("run_python", "get_document_state", "make_drawing", "get_view"))
          and not [t for t in projects.OLD_TOOLS if t.encode() in new("CLAUDE.md")]
          and b"make_drawing" in new(".claude/commands/drawing.md"))

    # ---- a project from the old template: replaced, the old files kept ----
    project = make("old")
    was = files(project)
    notes = projects.migrate(project)
    now = files(project)
    check("old project: .mcp.json is the new one, the old one is kept as .mcp.json.old",
          now[".mcp.json"] == new(".mcp.json") and now.get(".mcp.json.old") == was[".mcp.json"])
    check("old project: CLAUDE.md, the commands and settings.json are replaced too, each with its .old",
          all(now[rel] == new(rel) and now.get(rel + ".old") == was[rel] for rel in changed), str(changed))
    check("old project: a file that did not change in the template is left as it is, without a copy",
          all(now[rel] == was[rel] and rel + ".old" not in now for rel in same), str(same))
    check("old project: nothing else in the project is touched (model, references, mode, session)",
          {k: v for k, v in now.items() if k not in changed and not k.endswith(".old")}
          == {k: v for k, v in was.items() if k not in changed})
    check("old project: no note, there is nothing to tell", notes == [], str(notes))
    check("old project: Claude Code does not read a command file with .old at its end",
          not [n for n in os.listdir(os.path.join(project, ".claude", "commands"))
               if n.endswith(".md") and n[:-3] not in ("build", "check", "drawing", "refs", "teach")])
    replaced = []
    notes = projects.migrate(project, replaced)
    check("a second run changes nothing and makes no second copy", files(project) == now and notes == []
          and replaced == [])
    project = make("old-told")
    replaced = []
    projects.migrate(project, replaced)
    check("migrate(project, replaced) names the files that it replaced, for the note in the chat",
          sorted(replaced) == changed, str(replaced))

    # ---- Windows: git wrote the template with CRLF line ends, and so has the project ----
    project = make("old-crlf", crlf=True)
    was = files(project)
    notes = projects.migrate(project)
    now = files(project)
    check("(setup) the files of this project have CRLF line ends", b"\r\n" in was[".mcp.json"]
          and was[".mcp.json"] != before[".mcp.json"].encode("utf-8"))
    check("CRLF: the files are known as the old template and replaced",
          all(now[rel] == new(rel) for rel in (".mcp.json", "CLAUDE.md", ".claude/commands/check.md",
                                               ".claude/commands/drawing.md")) and notes == [], str(notes))
    check("CRLF: the kept copy is the file as it was, byte for byte",
          now[".mcp.json.old"] == was[".mcp.json"] and now["CLAUDE.md.old"] == was["CLAUDE.md"])

    # ---- the first versions of two files ----
    project = make("oldest", older)
    was = files(project)
    notes = projects.migrate(project)
    now = files(project)
    check("the first settings.json (it asked for web tools) and the first drawing command are replaced too",
          now[".claude/settings.json"] == new(".claude/settings.json")
          and now[".claude/settings.json.old"] == was[".claude/settings.json"]
          and now[".claude/commands/drawing.md"] == new(".claude/commands/drawing.md")
          and now[".claude/commands/drawing.md.old"] == was[".claude/commands/drawing.md"] and notes == [], str(notes))
    project = make("oldest-opened", older)
    runner.allow_web_in_project(project)                 # what the panel did to such a project before
    rewritten = read(os.path.join(project, ".claude", "settings.json"))
    check("a first settings.json that the panel rewrote before is the second version (it accepted edits)",
          rewritten == before[".claude/settings.json"].encode("utf-8") and b"acceptEdits" in rewritten)
    projects.migrate(project)
    check("... and is replaced too, with its .old",
          read(os.path.join(project, ".claude", "settings.json")) == new(".claude/settings.json")
          and read(os.path.join(project, ".claude", "settings.json.old")) == rewritten)

    # ---- files that the user changed: never touched ----
    mine = {".mcp.json": before[".mcp.json"].replace('"mcpServers": {', '"mcpServers": {\n    "other": {"command": "x"},'),
            "CLAUDE.md": before["CLAUDE.md"] + "\n## My shop\n\nWe only have M6 and M8 screws.\n",
            ".claude/settings.json": before[".claude/settings.json"].replace('"Bash",', '"Bash", "WebFetch",'),
            ".claude/commands/drawing.md": before[".claude/commands/drawing.md"] + "Always A4.\n",
            ".claude/commands/check.md": before[".claude/commands/check.md"].replace("7.85", "2.70"),
            ".claude/commands/build.md": before[".claude/commands/build.md"] + "Steel S235 if I say nothing.\n",
            ".claude/commands/teach.md": before[".claude/commands/teach.md"] + "In German.\n"}
    project = make("edited", mine)
    was = files(project)
    notes = projects.migrate(project)
    now = files(project)
    check("(setup) each of these files differs from the old template", all(mine[rel] != before[rel] for rel in mine))
    check("edited project: not one byte of the project changed, and no .old file was made", now == was,
          str(sorted(set(now) ^ set(was))))
    check("edited project: one note, in plain words", len(notes) == 1 and all("Traceback" not in n for n in notes),
          str(notes))
    check("edited project: no note about the .mcp.json: the panel gives Claude Code the one of the addon",
          not [n for n in notes if ".mcp.json" in n], str(notes))
    check("edited project: the note says that CLAUDE.md names old tools, and names the new ones",
          len(notes) == 1 and "`CLAUDE.md`" in notes[0] and "old" in notes[0] and "`list_documents`" in notes[0]
          and all(t in notes[0] for t in ("run_python", "get_document_state", "make_drawing", "get_view")),
          str(notes))
    check("edited project: asked again, the same notes and still no change",
          projects.migrate(project) == notes and files(project) == was)

    own = "# My rules\n\nMillimetres. Ask before you delete.\n"
    project = make("own-rules", {"CLAUDE.md": own})
    notes = projects.migrate(project)
    check("a CLAUDE.md of the user that names no old tool: left alone, and no note about it",
          read(os.path.join(project, "CLAUDE.md")) == own.encode() and notes == []
          and not os.path.exists(os.path.join(project, "CLAUDE.md.old")), str(notes))
    project = make("one-space", {".mcp.json": before[".mcp.json"] + " "})
    projects.migrate(project)
    check("one more space at the end is a change of the user: the file stays",
          read(os.path.join(project, ".mcp.json")) == (before[".mcp.json"] + " ").encode())

    # ---- files that are not there ----
    project = make("deleted")
    for rel in (".mcp.json", "CLAUDE.md", ".claude/commands/check.md"):
        os.remove(os.path.join(project, *rel.split("/")))
    notes = projects.migrate(project)
    check("a project without CLAUDE.md gets the new one (the note says: delete it to get the new one)",
          read(os.path.join(project, "CLAUDE.md")) == new("CLAUDE.md") and notes == []
          and not os.path.exists(os.path.join(project, "CLAUDE.md.old")), str(notes))
    check("a .mcp.json that the user deleted does not come back: the panel does not need it",
          not os.path.lexists(os.path.join(project, ".mcp.json")))
    check("a command that the user deleted does not come back",
          not os.path.exists(os.path.join(project, ".claude", "commands", "check.md")))

    # ---- a .old file that is there already is not written over ----
    project = make("kept")
    for rel in (".mcp.json.old", ".mcp.json.old-2"):
        with open(os.path.join(project, rel), "w", encoding="utf-8") as f:
            f.write("the user's own copy " + rel)
    projects.migrate(project)
    check("a .old file of the user stays; the old template goes to the next free name",
          read(os.path.join(project, ".mcp.json.old")) == b"the user's own copy .mcp.json.old"
          and read(os.path.join(project, ".mcp.json.old-2")) == b"the user's own copy .mcp.json.old-2"
          and read(os.path.join(project, ".mcp.json.old-3")) == before[".mcp.json"].encode()
          and read(os.path.join(project, ".mcp.json")) == new(".mcp.json"))

    # ---- a project of today, and no project at all ----
    project = projects.create("Made today")
    was = files(project)
    notes = projects.migrate(project)
    check("a project from the template of today: nothing changes, no note", files(project) == was and notes == [],
          str(notes))
    check("a folder that is not there: no error, no folder made",
          projects.migrate(os.path.join(home, "projects", "nothing-here")) == []
          and not os.path.exists(os.path.join(home, "projects", "nothing-here")))
    project = make("read-only")
    path = os.path.join(project, ".mcp.json")
    real_replace, os.replace = os.replace, lambda a, b: (_ for _ in ()).throw(PermissionError("in use"))
    try:
        notes = projects.migrate(project)                # Windows: a file that another program has open
    finally:
        os.replace = real_replace
    check("a file that cannot be replaced: no error, the old file stays whole, and the note says it is old",
          read(path) == before[".mcp.json"].encode() and not os.path.exists(path + ".new")
          and not os.path.exists(path + ".old") and any("`CLAUDE.md`" in n for n in notes)
          and read(os.path.join(project, "CLAUDE.md")) == before["CLAUDE.md"].encode()
          and not [n for n in files(project) if n.endswith((".new", ".old"))], str(notes))

    # ---- a project folder from another person: names that are taken, and links that lead out ----
    project = make("draft")
    with open(os.path.join(project, "CLAUDE.md.new"), "w", encoding="utf-8") as f:
        f.write("my draft of the next CLAUDE.md")
    projects.migrate(project)
    check("a file <name>.new of the user is not written over and not moved away: the update uses a free name",
          read(os.path.join(project, "CLAUDE.md.new")) == b"my draft of the next CLAUDE.md"
          and read(os.path.join(project, "CLAUDE.md")) == new("CLAUDE.md")
          and read(os.path.join(project, "CLAUDE.md.old")) == before["CLAUDE.md"].encode()
          and not os.path.lexists(os.path.join(project, "CLAUDE.md.new-2")))

    outside = os.path.join(home, "outside")
    os.makedirs(outside)

    def link(target, name, folder=False):
        """A link, or False where this system or this user cannot make one (Windows)."""
        try:
            os.symlink(target, name, target_is_directory=folder)
            return True
        except (OSError, NotImplementedError, AttributeError):
            return False

    keys = os.path.join(outside, "authorized_keys")
    with open(keys, "w", encoding="utf-8") as f:
        f.write("the user's own file outside the project")
    project = make("links")
    can_link = (link(keys, os.path.join(project, "CLAUDE.md.new"))
                and link(os.path.join(outside, "not-there-yet"), os.path.join(project, ".mcp.json.new"))
                and link(keys, os.path.join(project, ".claude", "settings.json.old")))
    if not can_link:
        print("skip  links: this system or this user cannot make a symbolic link", flush=True)
    else:
        projects.migrate(project)
        check("<name>.new is a link to a file outside the project: that file is not written",
              read(keys) == b"the user's own file outside the project"
              and os.path.islink(os.path.join(project, "CLAUDE.md.new")))
        check("... and the project still gets its new files, which are files and not links",
              read(os.path.join(project, "CLAUDE.md")) == new("CLAUDE.md")
              and not os.path.islink(os.path.join(project, "CLAUDE.md"))
              and read(os.path.join(project, "CLAUDE.md.old")) == before["CLAUDE.md"].encode()
              and read(os.path.join(project, ".mcp.json")) == new(".mcp.json")
              and not os.path.islink(os.path.join(project, ".mcp.json")))
        check("a link to a file that is not there makes no file outside the project",
              os.listdir(outside) == ["authorized_keys"], str(os.listdir(outside)))
        check("<name>.old is a link: the old file is kept under the next free name",
              read(os.path.join(project, ".claude", "settings.json.old-2")) == before[".claude/settings.json"].encode()
              and read(os.path.join(project, ".claude", "settings.json")) == new(".claude/settings.json"))

        # the .claude folder of the project is a link to a folder somewhere else
        shared = os.path.join(outside, "shared-claude")
        project = make("linked-folder")
        shutil.move(os.path.join(project, ".claude"), shared)
        was_shared = files(shared)
        link(shared, os.path.join(project, ".claude"), folder=True)
        projects.migrate(project)
        check("a .claude folder that is a link to another folder: nothing is changed or made there",
              files(shared) == was_shared and b"acceptEdits" in was_shared["settings.json"],
              str(sorted(set(files(shared)) ^ set(was_shared))))
        check("... the files in the project's own folder are still brought up to date",
              read(os.path.join(project, "CLAUDE.md")) == new("CLAUDE.md"))

        # the project folder itself is a link in the projects folder: that is the user's own choice
        real_place = make("real-place")
        shutil.move(real_place, os.path.join(outside, "real-place"))
        link(os.path.join(outside, "real-place"), real_place, folder=True)
        projects.migrate(real_place)
        check("a project folder that is a link itself is brought up to date as every other",
              read(os.path.join(real_place, ".claude", "settings.json")) == new(".claude/settings.json")
              and read(os.path.join(real_place, "CLAUDE.md")) == new("CLAUDE.md"))
    shutil.rmtree(os.path.join(home, "projects"), ignore_errors=True)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILED.append("the test script itself")
print(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
      + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"), flush=True)
os._exit(1 if FAILED else 0)
