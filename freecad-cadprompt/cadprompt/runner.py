"""Runs the user's own Claude Code headless and turns its stream into signals.

One `claude -p` process per message, in the project folder, prompt on stdin,
`--output-format stream-json --verbose`. The conversation continues with
`--resume <session_id>`. Claude Code reads the project's .mcp.json,
.claude/settings.json and CLAUDE.md exactly as it does in a terminal, and
signs in with the user's own login: this addon never sees credentials.
"""

import json
import os

from PySide import QtCore

from . import paths


HIDDEN_TOOLS = ("ToolSearch",)   # Claude Code housekeeping, not something the user did


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


def backend_env():
    """Extra environment for a custom (local) model endpoint, or {} for the Claude account."""
    p = paths.params()
    if p.GetString("Backend", "account") != "custom":
        return {}
    url = p.GetString("BaseUrl", "")
    if not url:
        return {}
    env = {"ANTHROPIC_BASE_URL": url,
           "ANTHROPIC_AUTH_TOKEN": p.GetString("AuthToken", "") or "local"}
    model = p.GetString("Model", "")
    if model:
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[k] = model
    return env


def _program(claude, args):
    if paths.WIN and claude.lower().endswith((".cmd", ".bat")):
        return "cmd.exe", ["/c", claude] + args
    return claude, args


class ClaudeRun(QtCore.QObject):
    text = QtCore.Signal(str)              # a block of Claude's answer (markdown)
    tool = QtCore.Signal(str, str)         # tool name, short detail
    tool_error = QtCore.Signal(str)        # a tool result that failed
    finished = QtCore.Signal(dict)         # the final result event (or a synthetic one)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self._buf = b""
        self._err = b""
        self._result = None
        self._tools = {}

    def running(self):
        return self.proc is not None and self.proc.state() != QtCore.QProcess.NotRunning

    def start(self, claude, project, prompt, session=None, env=None):
        args = ["-p", "--output-format", "stream-json", "--verbose"]
        if session:
            args += ["--resume", session]
        model = paths.params().GetString("ModelOverride", "")
        if model and paths.params().GetString("Backend", "account") == "account":
            args += ["--model", model]
        if paths.params().GetBool("AllowWeb", False):
            args += ["--allowedTools", "WebSearch", "WebFetch"]

        pe = QtCore.QProcessEnvironment.systemEnvironment()
        extra_path = [os.path.dirname(claude)]
        uvx = paths.find_tool("uvx")
        if uvx:
            extra_path.append(os.path.dirname(uvx))
        pe.insert("PATH", os.pathsep.join(extra_path + [pe.value("PATH", "")]))
        for k, v in (env or {}).items():
            pe.insert(k, v)

        self._buf, self._err, self._result, self._tools = b"", b"", None, {}
        self.proc = QtCore.QProcess(self)
        self.proc.setWorkingDirectory(project)
        self.proc.setProcessEnvironment(pe)
        self.proc.readyReadStandardOutput.connect(self._on_out)
        self.proc.readyReadStandardError.connect(self._on_err)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        program, argv = _program(claude, args)
        self.proc.start(program, argv)
        self.proc.write(prompt.encode("utf-8"))
        self.proc.closeWriteChannel()

    def stop(self):
        if self.running():
            self.proc.kill()

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
                if block.get("type") == "tool_result" and block.get("is_error"):
                    self.tool_error.emit(self._tools.get(block.get("tool_use_id"), "tool"))
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
        if res is None:
            stderr = self._err.decode("utf-8", "replace").strip()
            res = {"is_error": True, "kind": "crash",
                   "result": stderr[-1500:] or f"Claude Code stopped (exit code {code})."}
        self.finished.emit(res)
        self.proc.deleteLater()
        self.proc = None


def auth_status(claude, callback, parent=None):
    """Ask `claude auth status` without blocking FreeCAD. callback(dict or None)."""
    proc = QtCore.QProcess(parent)
    pe = QtCore.QProcessEnvironment.systemEnvironment()
    for k, v in backend_env().items():
        pe.insert(k, v)
    proc.setProcessEnvironment(pe)

    def done(_code, _status):
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        try:
            callback(json.loads(out))
        except ValueError:
            callback(None)
        proc.deleteLater()

    proc.finished.connect(done)
    program, argv = _program(claude, ["auth", "status"])
    proc.start(program, argv)
    return proc


WEB_TOOLS = ("WebSearch", "WebFetch")


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
    with open(path, "w", encoding="utf-8") as f:
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
        return (m.group(1) + " " + (args.group(1) if args else "")).strip()
    if content.lstrip().startswith("<"):
        return None
    return content.strip()


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
