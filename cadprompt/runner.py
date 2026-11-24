"""Runs the user's own Claude Code headless and turns its stream into signals.

One `claude -p` process per message, in the project folder, prompt on stdin,
`--output-format stream-json --verbose`. The conversation continues with
`--resume <session_id>`. Claude Code reads the project's CLAUDE.md and
.claude/ folder as it does in a terminal, and signs in with the user's own
login, or with the endpoint chosen in Settings (backends.claude_env).

What does not come from the project folder, because a project can come from
another person: the list of MCP servers (mcp_config: the file of the addon,
which sends Claude Code to the FreeCAD tools of this process, bridge.py;
mcp_env() is where and with which token), the permission mode and the shell
(ClaudeRun.start). A project folder with hooks is not started at all
(project_hooks).
"""

import json
import os
import re

from PySide import QtCore

from . import backends, consent, paths


HIDDEN_TOOLS = ("ToolSearch",)   # Claude Code housekeeping, not something the user did
MCP_SERVER = "freecad"           # the name of the bridge in the .mcp.json of the addon
LOCAL_HOSTS = ("127.0.0.1", "localhost")   # the bridge, and a model server on this computer
WEB_TOOLS = ("WebSearch", "WebFetch")
# Claude Code's own tools that change a file. Started with -p it cannot ask anybody, so each
# call was refused (--permission-mode default), and Claude Code asked the user for a permission
# that nobody can give (measured 2026-11-24 with 2.1.331). Now they are not in its tools
# (READ_TOOLS), and template/CLAUDE.md tells it to save a file of the project with run_python,
# where the user says Run or Skip. The panel says the same to the user should one of these
# be refused all the same.
FILE_TOOLS = ("Write", "Edit", "NotebookEdit")
# Claude Code's own tools that the panel gives it: the ones that read a file of the project. With
# --tools the others (its agents, its timers, its tool search) are not there at all, so they
# take no room in every request and Claude Code cannot try one that nobody could approve.
READ_TOOLS = ("Read", "Glob", "Grep")
# Claude Code loads the tools of a server only after a search of its own ("ToolSearch"), which is
# one more round trip to the service before the first step. With this variable set to "false"
# the six FreeCAD tools are there from the start (read in the text of Claude Code 2.1.331).
NO_TOOL_SEARCH = "ENABLE_TOOL_SEARCH"
STATE = "cadprompt-state"        # the tag around what the panel puts under a message of the user
# How long Claude thinks before each step (the Claude account only). Measured 2026-11-24 with
# Claude Code 2.1.331 and Opus 5.5, a plate with four holes: its own setting 44 s, medium 23 s,
# low 19 s. A bent bracket: medium 58 s and right the first time, low 66 s with one failed
# step. So medium is what a new profile has. OWN_EFFORT leaves it to Claude Code's settings.
EFFORTS = ("low", "medium", "high")
EFFORT, OWN_EFFORT = "medium", "own"
NO_STORED_TRUST = "CLAUDE_CODE_IGNORE_PERSISTED_TRUST"   # Claude Code: no "trust this folder" answer counts
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
HOOKS = ("Claude Code was not started: this project has hooks in %s. A hook is a command that Claude Code "
         "runs by itself, without a question, and a project can come from another person. Open the file and "
         "take the \"hooks\" part out, then send your message again.")
NOT_ALLOWED = "Claude Code was not started: you did not allow it yet. Send your message again and say Allow."


def allowed():
    """True when the user said yes to Claude Code for the source that is chosen now.

    Claude Code works with a login found on this computer and sends the
    conversation to a service. The panel asks before (Panel._consent, which
    is backends.may_contact); this is the lock on the two doors themselves,
    ClaudeRun.start and auth_status. It asks nothing.
    """
    service = backends.service()
    return bool(service) and service[0] in consent.CLAUDE_CODE and consent.allowed(*service)


def project_hooks(project):
    """The files of a project folder that give Claude Code hooks, as paths inside the project.

    A hook is a shell command that Claude Code runs by itself: at its start,
    before a tool, after a tool. Started with -p it runs the hooks of the
    project's settings files without a question, and a hook can also say yes
    to a tool. A project can come from another person, so ClaudeRun.start
    does not start Claude Code in a folder that has one. The settings files
    are looked at, and the commands, agents and skills in .claude/ (a hook can
    be in the head of such a file).
    """
    found = []
    for rel in SETTINGS_FILES:
        try:
            with open(os.path.join(project, *rel.split("/")), encoding="utf-8", errors="replace") as f:
                text = f.read(2_000_000)
        except OSError:
            continue
        try:
            data = json.loads(text)
            hooks = isinstance(data, dict) and bool(data.get("hooks"))
        except ValueError:
            hooks = "hooks" in text      # not JSON for us: Claude Code can read it all the same
        if hooks:
            found.append(rel)
    seen = 0
    for folder, _dirs, files in os.walk(os.path.join(project, ".claude")):
        for name in sorted(files):
            seen += 1
            if seen > 2000:              # not a project of ours: stop looking, the files so far count
                return found
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read(20_000)
            except OSError:
                continue
            head = re.match(r"\ufeff?---\r?\n(.*?)\r?\n---", text, re.S)
            if head and re.search(r"^\s*hooks\s*:", head.group(1), re.M):
                found.append(os.path.relpath(path, project).replace(os.sep, "/"))
    return found


def with_state(text, state):
    """The message that Claude Code gets: the user's words, then the state of the project.

    `state` is bridge.state(): the mode, the references, the model files and the open
    document. Claude Code needed three round trips to the service to read them, before
    its first step (measured 2026-11-24: 7 of 56 seconds). The state comes after the words:
    /build and the other commands must stay at the start, and the conversation before
    this message stays the same text, so the service can use its cache.

    The state holds names from the document and text from refs/INDEX.md, and both can come
    from another person. The closing tag is taken out of them, so nothing in the state can
    end the block and go on as words of the user.
    """
    state = (state or "").strip()
    if not state:
        return text
    state = state.replace(STATE + ">", STATE + " >")
    return "%s\n\n<%s>\n%s\n</%s>" % (text.rstrip(), STATE, state, STATE)


def without_state(text):
    """The words of the user, without the state that with_state() put under them."""
    rest = text.rstrip()
    if not rest.endswith("</%s>" % STATE):
        return text
    at = rest.rfind("<%s>" % STATE)
    return rest[:at].rstrip() if at >= 0 else text


def effort():
    """The thinking effort from Settings: one of EFFORTS, or "" for Claude Code's own setting."""
    value = paths.params().GetString("Effort", EFFORT).strip().lower()
    if value == OWN_EFFORT:
        return ""
    return value if value in EFFORTS else EFFORT


def own_tools():
    """Claude Code's own tools in the panel: reading, and the web when the user allows it."""
    return list(READ_TOOLS) + (list(WEB_TOOLS) if paths.params().GetBool("AllowWeb", False) else [])


def _short_tool(name):
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _tool_detail(inp):
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "name", "obj_name", "doc_name", "label", "path", "url", "query"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            return os.path.basename(v) if key == "file_path" else v
    code = inp.get("code")
    if isinstance(code, str) and code.strip():
        first = code.strip().splitlines()[0]
        return first[:60] + ("…" if len(first) > 60 else "")
    return ""


def mcp_env(url, token):
    """The variables that the .mcp.json of the addon reads: where the FreeCAD tools are.

    Not names that start with ANTHROPIC_: Claude Code reads those as empty
    when it fills in the address and the headers of a server.
    """
    return {"CADPROMPT_MCP_URL": url, "CADPROMPT_MCP_TOKEN": token}


def mcp_config():
    """The file that tells Claude Code where the FreeCAD tools are: the one of the addon.

    Never the .mcp.json of the project folder. A project can come from another
    person, and Claude Code started with -p uses the .mcp.json of its folder
    without a question: a program named there would start as the user, with
    the token of the bridge in its environment. With --strict-mcp-config
    Claude Code reads this file and no other, so `freecad` in allowed_tools()
    is this FreeCAD. A file and not JSON text: _cmd_line() takes the quotes
    out of an argument. The file holds no secret; see mcp_env().
    """
    folder = paths.template_dir()
    return os.path.join(folder, ".mcp.json") if folder else None


def allowed_tools():
    """The tools that Claude Code may call without a question of its own.

    Claude Code started with -p cannot ask anybody, and it does not use the
    allow rules of a project folder that was never opened in a terminal. So
    the FreeCAD tools of the bridge are named here, each by its full name and
    no others. The user's yes or no to a step is asked inside FreeCAD, before
    the tool runs (approval.request).
    """
    from . import local_prompt
    tools = ["mcp__%s__%s" % (MCP_SERVER, name) for name in local_prompt.BRIDGE]
    if paths.params().GetBool("AllowWeb", False):
        tools += list(WEB_TOOLS)
    return tools


def backend_env():
    """Extra environment for a custom endpoint, or {} for every other source."""
    return backends.custom_env()


def _apply_backend(pe):
    """Fit the environment to the source chosen in Settings.

    Removing matters as much as setting: an ANTHROPIC_API_KEY left in the
    user's shell must not bill the API when the Claude account is chosen, and
    a provider switch there (CLAUDE_CODE_USE_BEDROCK ...) must not send the
    conversation to a service that the user was not asked for.
    """
    add, drop = backends.claude_env(pe.keys())
    for k in drop:
        pe.remove(k)
    for k, v in add.items():
        pe.insert(k, v)


def system_environment():
    """The environment of this FreeCAD, without what FreeCAD's own start script put in.

    An AppImage, the macOS app and other bundles export PYTHONHOME and more for
    their own Python. Handed on, no other Python would start, for example in a
    tool of Claude Code. See paths.child_environment.
    """
    pe = QtCore.QProcessEnvironment.systemEnvironment()
    ours = paths.child_environment({k: pe.value(k) for k in pe.keys()})
    for k in pe.keys():
        if k not in ours:
            pe.remove(k)
        elif ours[k] != pe.value(k):
            pe.insert(k, ours[k])
    return pe


def _no_proxy_for_local(pe):
    """Put this computer into NO_PROXY, and keep what the user has there.

    Claude Code sends the requests to an http MCP server through the proxy of
    HTTP_PROXY / HTTPS_PROXY, also for 127.0.0.1, unless NO_PROXY names the
    host. The proxy would get the token of the bridge and the code of
    run_python in clear text, and it cannot reach the 127.0.0.1 of this
    computer: no FreeCAD tool would work. The two spellings get the same list:
    Claude Code joins them, other programs read only one of them.
    """
    have = []
    for name in ("no_proxy", "NO_PROXY"):
        for entry in pe.value(name, "").replace(",", " ").split():
            if entry not in have:
                have.append(entry)
    if "*" in have:      # "no proxy for anything": more entries would make it a list of host names
        return
    value = ",".join(have + [host for host in LOCAL_HOSTS if host not in have])
    for name in ("no_proxy", "NO_PROXY"):
        pe.insert(name, value)


def claude_environment(claude, env=None):
    """The environment Claude Code starts with: the system's, our PATH, env, the source."""
    pe = system_environment()
    pe.insert("PATH", os.pathsep.join([os.path.dirname(claude), pe.value("PATH", "")]))
    for k, v in (env or {}).items():
        pe.insert(k, v)
    _apply_backend(pe)
    _no_proxy_for_local(pe)
    # A project can come from another person. With this variable Claude Code started with -p
    # takes the folder as one that nobody trusted: the hooks of the project's settings files
    # do not run and their allow rules do not count, also when the folder (or the git checkout
    # around it) was trusted in a terminal before. CLAUDE.md and the commands are still read.
    # (Read in the text of Claude Code 2.1.327. An older version that does not know it ignores it.)
    pe.insert(NO_STORED_TRUST, "1")
    pe.insert(NO_TOOL_SEARCH, "false")      # the FreeCAD tools from the start: see NO_TOOL_SEARCH
    return pe


def _is_script(claude):
    """A .cmd file (Claude Code from npm on Windows): only cmd.exe can run it."""
    return paths.WIN and claude.lower().endswith((".cmd", ".bat"))


def _cmd_line(claude, args):
    """The text for `cmd.exe /d /s /c "<text>"`.

    With /s, cmd.exe takes everything between the first and the last quote as
    it is. Without it, a path with a space (C:\\Users\\Ann Lee\\...) plus one more
    quoted argument makes cmd.exe cut the line at the wrong quotes.
    Every word is in quotes, so a space, & or ( in it stays text.
    """
    return '/d /s /c "%s"' % " ".join('"%s"' % word.replace('"', "") for word in [claude] + args)


def _start(proc, claude, args):
    if not _is_script(claude):
        proc.start(claude, args)
    elif hasattr(proc, "setNativeArguments"):
        proc.setNativeArguments(_cmd_line(claude, args))
        proc.start(os.environ.get("ComSpec") or "cmd.exe", [])
    else:
        proc.start(os.environ.get("ComSpec") or "cmd.exe", ["/d", "/c", claude] + args)


def _kill(proc):
    """End the program, and on Windows what it started.

    Behind a .cmd file the real program is a child of cmd.exe. Ending cmd.exe
    alone would leave it running, and working on the model, after Stop.
    taskkill is a part of Windows; /T takes the child processes too. It needs
    the first process alive to find the others, so that one is ended last.
    """
    pid = proc.processId() if paths.WIN else 0
    if not pid:
        proc.kill()
        return
    killer = QtCore.QProcess(proc)       # a child of proc: gone when proc is

    def after(*_):
        try:
            if proc.state() != QtCore.QProcess.NotRunning:
                proc.kill()
        except RuntimeError:      # Qt deleted the process object already: nothing is left to end
            pass
    killer.finished.connect(after)
    killer.errorOccurred.connect(after)
    killer.start("taskkill", ["/PID", str(pid), "/T", "/F"])


class ClaudeRun(QtCore.QObject):
    text = QtCore.Signal(str)              # a block of Claude's answer (markdown)
    tool = QtCore.Signal(str, str)         # tool name, short detail
    tool_error = QtCore.Signal(str)        # a tool result that failed
    tool_done = QtCore.Signal(str)         # the result of a tool went back: the AI reads it now
    finished = QtCore.Signal(dict)         # the final result event (or a synthetic one)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self._buf = b""
        self._err = b""
        self._result = None
        self._tools = {}
        self._stopped = False

    def running(self):
        return self.proc is not None and self.proc.state() != QtCore.QProcess.NotRunning

    def start(self, claude, project, prompt, session=None, env=None):
        if not allowed():
            self.finished.emit({"is_error": True, "result": NOT_ALLOWED, "kind": "start"})
            return
        hooks = project_hooks(project)
        if hooks:
            names = ", ".join("`%s`" % rel for rel in hooks[:5])
            self.finished.emit({"is_error": True, "result": HOOKS % names, "kind": "start"})
            return
        args = ["-p", "--output-format", "stream-json", "--verbose"]
        if session:
            args += ["--resume", session]
        model = paths.params().GetString("ModelOverride", "")
        if model and backends.current() == "account":
            args += ["--model", model]
        if effort() and backends.current() == "account":     # another endpoint may not know the word
            args += ["--effort", effort()]
        # Three things that a file of the project folder must not decide (it can come from
        # another person, and nobody can answer a question of Claude Code started with -p):
        # the MCP servers are those of the addon's file and no others; with no file, none.
        config = mcp_config()
        args += (["--mcp-config", config] if config else []) + ["--strict-mcp-config"]
        # Its own tools are the reading ones and no others (READ_TOOLS). Should one more be
        # there all the same: Claude Code asks before it changes a file (that is a no here),
        # and it has no shell.
        args += ["--tools", ",".join(own_tools())]
        args += ["--permission-mode", "default", "--disallowedTools", "Bash"]
        args += ["--allowedTools"] + allowed_tools()

        self._buf, self._err, self._result, self._tools = b"", b"", None, {}
        self._stopped = False
        self.proc = QtCore.QProcess(self)
        self.proc.setWorkingDirectory(project)
        self.proc.setProcessEnvironment(claude_environment(claude, env))
        self.proc.readyReadStandardOutput.connect(self._on_out)
        self.proc.readyReadStandardError.connect(self._on_err)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        _start(self.proc, claude, args)
        self.proc.write(prompt.encode("utf-8"))
        self.proc.closeWriteChannel()

    def stop(self):
        if self.running():
            self._stopped = True
            _kill(self.proc)

    # ---- stream ---------------------------------------------------------
    def _on_err(self):
        self._err = (self._err + bytes(self.proc.readAllStandardError()))[-8000:]

    def _on_out(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                try:
                    self._event(json.loads(line))
                except ValueError:
                    pass

    def _event(self, ev):
        t = ev.get("type")
        if t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.text.emit(block["text"])
                elif block.get("type") == "tool_use" and block.get("name") not in HIDDEN_TOOLS:
                    name = _short_tool(block.get("name", "tool"))
                    self._tools[block.get("id")] = name
                    self.tool.emit(name, _tool_detail(block.get("input")))
        elif t == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result" or block.get("tool_use_id") not in self._tools:
                    continue
                if block.get("is_error"):
                    self.tool_error.emit(self._tools[block.get("tool_use_id")])
                self.tool_done.emit(self._tools[block.get("tool_use_id")])
        elif t == "result":
            self._result = ev

    def _on_error(self, err):
        if err == QtCore.QProcess.FailedToStart:
            self.finished.emit({"is_error": True, "result": "Claude Code could not be started.",
                                "kind": "start"})

    def _on_finished(self, code, _status):
        self._on_out()
        if self._buf.strip():
            try:
                self._event(json.loads(self._buf))
            except ValueError:
                pass
        res = self._result
        if res is None and self._stopped:
            # ended by stop(): not an error. The same answer as the built-in engine gives.
            res = {"is_error": False, "stopped": True, "result": ""}
        if res is None:
            stderr = self._err.decode("utf-8", "replace").strip()
            res = {"is_error": True, "kind": "crash",
                   "result": stderr[-1500:] or f"Claude Code stopped (exit code {code})."}
        # let go of this process first: a listener can start the next run from inside finished
        proc, self.proc = self.proc, None
        try:
            self.finished.emit(res)
        finally:
            proc.deleteLater()


def auth_status(claude, callback, parent=None):
    """Ask `claude auth status` without blocking FreeCAD. callback(dict or None).

    Not before the user's yes to Claude Code (allowed()): then nothing is
    started, the callback is not called and None is returned.
    """
    if not allowed():
        return None
    proc = QtCore.QProcess(parent)
    pe = system_environment()
    _apply_backend(pe)
    proc.setProcessEnvironment(pe)

    def done(_code, _status):
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        try:
            callback(json.loads(out))
        except ValueError:
            callback(None)
        proc.deleteLater()

    proc.finished.connect(done)
    _start(proc, claude, ["auth", "status"])
    return proc


def allow_web_in_project(project):
    """Old projects ask for web tools; a question nobody can answer is a refusal.

    Drop WebSearch/WebFetch from permissions.ask so the panel's setting decides
    (the terminal still asks, since unlisted web tools need approval anyway).
    """
    path = os.path.join(project, ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return
    ask = s.get("permissions", {}).get("ask")
    if not isinstance(ask, list) or not any(t in ask for t in WEB_TOOLS):
        return
    s["permissions"]["ask"] = [t for t in ask if t not in WEB_TOOLS]
    if not s["permissions"]["ask"]:
        del s["permissions"]["ask"]
    # newline: the template has LF on every system; Windows would write CRLF here
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(s, f, indent=2)
        f.write("\n")


def _projects_dir(hint=None):
    if hint and os.path.isdir(hint):
        return hint
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(paths.HOME, ".claude")
    return os.path.join(base, "projects")


def transcript_path(project, session, projects_dir=None):
    import glob
    import re
    root = _projects_dir(projects_dir)
    mangled = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(project))
    p = os.path.join(root, mangled, session + ".jsonl")
    if os.path.isfile(p):
        return p
    found = glob.glob(os.path.join(root, "*", session + ".jsonl"))
    return found[0] if found else None


_CMD = None


def _user_text(content):
    """What the user typed, or None for tool results and internal messages."""
    global _CMD
    import re
    if _CMD is None:
        _CMD = (re.compile(r"<command-name>(.*?)</command-name>", re.S),
                re.compile(r"<command-args>(.*?)</command-args>", re.S))
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = "\n".join(texts) if texts and len(texts) == len(content) else None
    if not isinstance(content, str) or not content.strip():
        return None
    m = _CMD[0].search(content)
    if m:
        args = _CMD[1].search(content)
        return (m.group(1) + " " + (without_state(args.group(1)) if args else "")).strip()
    if content.lstrip().startswith("<"):
        return None
    return without_state(content).strip()


def read_history(path, limit=200):
    """(kind, text) entries from a Claude Code transcript: user, claude, tool."""
    out = []
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return out
    with f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("isSidechain") or ev.get("isMeta"):
                continue
            t = ev.get("type")
            content = (ev.get("message") or {}).get("content")
            if t == "user":
                text = _user_text(content)
                if text:
                    out.append(("user", text))
            elif t == "assistant" and isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        out.append(("claude", b["text"]))
                    elif b.get("type") == "tool_use" and b.get("name") not in HIDDEN_TOOLS:
                        out.append(("tool", f"`{_short_tool(b.get('name', 'tool'))}` {_tool_detail(b.get('input'))}".strip()))
    return out[-limit:]
