"""The chat panel: a dock in FreeCAD where the user says what to build.

Who does the work is chosen in Settings (backends.py): Claude Code with the
user's account, an API key or another endpoint, or the built-in engine with a
local model. The panel looks and works the same for all of them.
"""

import os

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtWidgets

from . import backends, bridge, paths, projects, runner

NEW = "+ New project…"

INSTALL_CLAUDE = {
    "win": "In PowerShell: `irm https://claude.ai/install.ps1 | iex`",
    "mac": "In Terminal: `curl -fsSL https://claude.ai/install.sh | bash`",
    "linux": "In a terminal: `curl -fsSL https://claude.ai/install.sh | bash`",
}
INSTALL_UV = {
    "win": 'In PowerShell: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`',
    "mac": "In Terminal: `curl -LsSf https://astral.sh/uv/install.sh | sh`",
    "linux": "In a terminal: `curl -LsSf https://astral.sh/uv/install.sh | sh`",
}


def _os_key():
    import sys
    return "win" if paths.WIN else "mac" if sys.platform == "darwin" else "linux"


class _Input(QtWidgets.QPlainTextEdit):
    send = QtCore.Signal()
    take = None   # set by the panel: take(mime) -> True when it became a reference

    def canInsertFromMimeData(self, mime):
        return mime.hasUrls() or mime.hasImage() or super().canInsertFromMimeData(mime)

    def insertFromMimeData(self, mime):
        if self.take and self.take(mime):
            return
        super().insertFromMimeData(mime)

    def keyPressEvent(self, e):
        if e.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and not (e.modifiers() & QtCore.Qt.ShiftModifier):
            self.send.emit()
            return
        super().keyPressEvent(e)


class _Source(QtWidgets.QLabel):
    """Which AI is active, on one line: a long model name is cut, not the dock widened."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.full = ""
        self.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def show_text(self, text):
        self.full = text
        self.setToolTip("The AI that does the work: " + text + ". Change it in Settings.")
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        self.setText(self.fontMetrics().elidedText(self.full, QtCore.Qt.ElideRight, self.width()))


class Panel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.run = runner.ClaudeRun(self)     # Claude Code: account, API key, other endpoint
        self.local_run = None                 # the built-in engine, made when first needed
        self._engine = self.run               # the one that got the last message
        self._connect(self.run)
        self._check = 0                       # counts check_setup runs: late answers are dropped
        self._stop_asked = False
        self.entries = []
        self.project = None
        self.claude = None
        self.claude_projects = None   # Claude Code's transcript folder, from auth status
        self.setAcceptDrops(True)
        self._last_prompt = None
        self._retried = False
        self._build_ui()
        self._show_source()
        self._fill_projects()
        QtCore.QTimer.singleShot(0, self.check_setup)

    # ---- the two engines ----------------------------------------------------
    def _connect(self, run):
        run.text.connect(lambda t: self._add("claude", t))
        run.tool.connect(self._on_tool)
        run.tool_error.connect(self._on_tool_error)
        run.finished.connect(lambda res, run=run: self._on_finished(res, run))

    def _local_engine(self):
        """The built-in engine, loaded on first use: the Claude Code sources never need it."""
        from . import local_engine
        return local_engine

    def _local_run(self):
        if self.local_run is None:
            self.local_run = self._local_engine().LocalRun(self)
            self._connect(self.local_run)
        return self.local_run

    def _running(self):
        return self.run.running() or (self.local_run is not None and self.local_run.running())

    def _stop(self):
        if self.run.running():
            self.run.stop()
        if self.local_run is not None and self.local_run.running():
            self.local_run.stop()

    def _who(self, start=False):
        """Who is working on the message that was sent."""
        if self._engine is self.run:
            return "Claude"
        return "The local model" if start else "the local model"

    def _ai(self, start=False):
        """Who the next message goes to."""
        if backends.uses_claude_code():
            return "Claude"
        return "The local model" if start else "the local model"

    def _show_source(self):
        """The label next to the mode buttons, and the tooltips that name the AI."""
        b = backends.current()
        p = paths.params()
        model = {"local": p.GetString("LocalModel", "").strip() or "no model yet",
                 "custom": p.GetString("Model", "").strip()}.get(b, p.GetString("ModelOverride", "").strip())
        self.source.show_text(backends.label(b) + (" · " + model if model else ""))
        self.build_btn.setToolTip(f"{self._ai(start=True)} builds the whole part")
        self.refs_btn.setToolTip(f"Copy photos, PDFs or drawings into refs/ for {self._ai()}")

    # ---- UI -------------------------------------------------------------
    def _build_ui(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        row = QtWidgets.QHBoxLayout()
        self.combo = QtWidgets.QComboBox()
        self.combo.setToolTip("Project: a folder with model/, refs/ and drawings/")
        self.combo.activated.connect(self._on_project_picked)
        row.addWidget(self.combo, 1)
        folder = QtWidgets.QToolButton(text="Folder")
        folder.setToolTip("Open the project folder")
        folder.clicked.connect(self._open_folder)
        row.addWidget(folder)
        gear = QtWidgets.QToolButton(text="Settings")
        gear.clicked.connect(self._settings)
        row.addWidget(gear)
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        self.build_btn = QtWidgets.QRadioButton("Build")
        self.teach_btn = QtWidgets.QRadioButton("Teach")
        self.teach_btn.setToolTip("One feature at a time: which tool, where, and why")
        self.build_btn.setChecked(True)
        self.build_btn.toggled.connect(self._on_mode)
        row.addWidget(self.build_btn)
        row.addWidget(self.teach_btn)
        self.source = _Source()
        row.addWidget(self.source, 1)
        newchat = QtWidgets.QToolButton(text="New chat")
        newchat.setToolTip("Start a fresh conversation in this project")
        newchat.clicked.connect(self._new_chat)
        row.addWidget(newchat)
        lay.addLayout(row)

        self.view = QtWidgets.QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view, 1)

        row = QtWidgets.QHBoxLayout()
        self.refs_btn = QtWidgets.QPushButton("Add reference…")
        self.refs_btn.clicked.connect(self._add_refs)
        row.addWidget(self.refs_btn)
        check = QtWidgets.QPushButton("Check")
        check.setToolTip("Review the model before it goes to the shop")
        check.clicked.connect(lambda: self.send("/check"))
        row.addWidget(check)
        drawing = QtWidgets.QPushButton("Drawing")
        drawing.setToolTip("DIN workshop drawing, PDF in drawings/: pick the sheet size")
        menu = QtWidgets.QMenu(drawing)
        for size in ("A2", "A3", "A4", "A5"):
            menu.addAction(size, lambda size=size: self.send(f"/drawing {size}"))
        drawing.setMenu(menu)
        row.addWidget(drawing)
        lay.addLayout(row)

        self.input = _Input()
        self.input.setPlaceholderText("Say what to build or change. Enter sends, Shift+Enter new line.")
        self.input.setMaximumHeight(90)
        self.input.send.connect(lambda: self.send())
        self.input.take = self.take_mime
        lay.addWidget(self.input)

        row = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send_or_stop)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

    def _add(self, kind, text):
        self.entries.append((kind, text))
        self._render()

    def _render(self):
        parts = []
        for kind, text in self.entries:
            if kind == "user":
                parts.append("> **You:** " + text.replace("\n", "\n> "))
            elif kind == "claude":
                parts.append(text)
            elif kind == "tool":
                parts.append(f"<small>▸ {text}</small>")
            elif kind in ("error", "setup"):
                parts.append(f"**⚠** {text}")
            else:
                parts.append(f"*{text}*")
        self.view.setMarkdown("\n\n".join(parts))
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _busy(self, on, msg=""):
        self.send_btn.setText("Stop" if on else "Send")
        self.status.setText(msg)

    # ---- setup checks -----------------------------------------------------
    def _setup(self, text, note=False):
        """A message of the setup check. The next check takes it away again."""
        self._add("setup-note" if note else "setup", text)

    def check_setup(self):
        """Say what is missing, in words a student can follow. True when ready."""
        self._check += 1
        if any(kind.startswith("setup") for kind, _ in self.entries):
            self.entries = [e for e in self.entries if not e[0].startswith("setup")]
            self._render()
        if backends.current() == "local":
            return self._check_local()
        return self._check_claude_code()

    def _check_local(self):
        """The built-in engine needs no Claude Code, no uv and no bridge: only a server and a model."""
        if not paths.template_dir():
            self._setup("The project template is missing: install the whole cadprompt repository.")
            return False
        cfg = backends.local_config()
        if not cfg["url"]:
            self._setup("No server address is set for the local model. Open Settings and enter it, "
                        f"for example `{backends.LOCAL_URL}` for LM Studio.")
            return False
        if not cfg["model"]:
            self._setup("No model is chosen yet. Open Settings, press Find models and pick one.")
            return False
        try:
            self._local_engine()
        except Exception as e:
            self._setup(f"The part of CADprompt that talks to local models did not load: {e}")
            return False
        return True

    def _check_claude_code(self):
        k = _os_key()
        backend = backends.current()
        if paths.in_flatpak():
            self._setup("This FreeCAD runs as a Flatpak and cannot start Claude Code from inside "
                        "its sandbox. Use the FreeCAD package or AppImage, or the terminal launcher, "
                        "or choose a local model in Settings.")
            return False
        if not paths.template_dir():
            self._setup("The project template is missing: install the whole cadprompt repository.")
            return False
        self.claude = paths.find_tool("claude")
        if not self.claude:
            then = " Then restart FreeCAD." if backend == "apikey" else \
                   " Then sign in once by running `claude` and restart FreeCAD."
            self._setup("Claude Code is not installed. " + INSTALL_CLAUDE[k] + then)
            return False
        if not paths.find_tool("uvx"):
            self._setup("uv is not installed (it runs the FreeCAD bridge). " + INSTALL_UV[k] +
                        " Then restart FreeCAD.")
            return False
        try:
            _, msg = bridge.ensure()
            if "expected" in msg:
                self._setup(msg, note=True)
        except LookupError:
            ans = QtWidgets.QMessageBox.question(
                self, "CADprompt",
                f"The FreeCAD MCP bridge (freecad-mcp {paths.FREECAD_MCP_VERSION}) is not installed.\n\n"
                "Download it from GitHub (github.com/neka-nat/freecad-mcp) into FreeCAD's Mod folder now?")
            if ans != QtWidgets.QMessageBox.Yes:
                self._setup("Without the bridge Claude cannot reach FreeCAD.")
                return False
            try:
                self._setup(bridge.install(), note=True)
                bridge.ensure()
            except Exception as e:
                self._setup(f"Could not install the bridge: {e}")
                return False
        except Exception as e:
            self._setup(f"The FreeCAD bridge did not start: {e}")
            return False
        if backend == "apikey":
            # the key signs in, so whether Claude Code has a login of its own does not matter
            if not backends.api_key():
                self._setup("No Anthropic API key is saved. Open Settings and paste your key.")
                return False
        elif not runner.backend_env():
            runner.auth_status(self.claude, lambda st, n=self._check: self._on_auth(st, n), self)
        return True

    def _on_auth(self, st, check=None):
        if check is not None and check != self._check:
            return      # the settings changed while Claude Code was answering
        if st and st.get("projectsDirectory"):
            self.claude_projects = st["projectsDirectory"]
            if len(self.entries) <= 1:
                self._load_history()
        if st is None:
            self._setup("Could not ask Claude Code whether you are signed in; trying anyway.", note=True)
        elif not st.get("loggedIn"):
            self._setup("Claude Code is not signed in. Open a terminal, run `claude`, sign in "
                        "with your Claude account, then try again here.")

    # ---- projects ---------------------------------------------------------
    def _fill_projects(self):
        self.combo.clear()
        names = projects.list_projects()
        last = paths.params().GetString("LastProject", "")
        self.combo.addItems(names + [NEW])
        if last in names:
            self.combo.setCurrentIndex(names.index(last))
            self._select(last, open_model=False)
        elif names:
            self._select(names[0], open_model=False)
        else:
            self.combo.setCurrentIndex(0)

    def _on_project_picked(self, i):
        name = self.combo.itemText(i)
        if name == NEW:
            text, ok = QtWidgets.QInputDialog.getText(self, "New project", "Project name:")
            if not ok or not text.strip():
                self._fill_projects()
                return
            try:
                path = projects.create(text.strip())
            except Exception as e:
                self._add("error", str(e))
                return
            name = os.path.basename(path)
            self.combo.blockSignals(True)
            self.combo.clear()
            self.combo.addItems(projects.list_projects() + [NEW])
            self.combo.setCurrentText(name)
            self.combo.blockSignals(False)
        self._select(name, open_model=True)

    def _select(self, name, open_model):
        self._stop()
        self.project = projects.path_of(name)
        paths.params().SetString("LastProject", name)
        runner.allow_web_in_project(self.project)
        self._sync_mode()
        self.entries = []
        self._add("note", f"Project **{name}**.")
        self._load_history()
        if open_model:
            model = projects.newest_model(self.project)
            if model:
                try:
                    projects.open_model(model)
                except Exception as e:
                    self._add("error", f"Could not open {os.path.basename(model)}: {e}")

    def _load_history(self):
        """The earlier conversation of this project with the source chosen now."""
        if not self.project:
            return
        if backends.current() == "local":
            try:
                old = self._local_engine().history(self.project)
            except Exception as e:
                App.Console.PrintWarning(f"CADprompt: could not read the local conversation: {e}\n")
                return
        else:
            sid = projects.get_session(self.project)
            path = sid and runner.transcript_path(self.project, sid, self.claude_projects)
            if not path:
                return
            old = runner.read_history(path)
        if old:
            self.entries = self.entries[:1] + old + [("note", "Continuing this conversation.")]
            self._render()

    def _sync_mode(self):
        """The mode file decides; /teach or /build typed in the chat change it too."""
        mode = projects.get_mode(self.project)
        for b in (self.build_btn, self.teach_btn):
            b.blockSignals(True)
        (self.teach_btn if mode == "teach" else self.build_btn).setChecked(True)
        for b in (self.build_btn, self.teach_btn):
            b.blockSignals(False)

    # ---- drag and drop, paste ---------------------------------------------
    def dragEnterEvent(self, e):
        m = e.mimeData()
        if m.hasUrls() or m.hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if self.take_mime(e.mimeData()):
            e.acceptProposedAction()

    def take_mime(self, mime):
        """Files, images and links dropped or pasted become references in refs/."""
        if not self.project:
            return False
        files, links = [], []
        if mime.hasUrls():
            for u in mime.urls():
                if u.isLocalFile() and os.path.isfile(u.toLocalFile()):
                    files.append(u.toLocalFile())
                elif u.scheme() in ("http", "https"):
                    links.append(u.toString())
        elif mime.hasText():
            t = mime.text().strip()
            if t.startswith(("http://", "https://")) and " " not in t and "\n" not in t:
                links.append(t)
        if not files and not links and mime.hasImage():
            import tempfile
            img = mime.imageData()
            tmp = os.path.join(tempfile.mkdtemp(prefix="cadprompt-"), "pasted.png")
            if img is not None and QtGui.QImage(img).save(tmp, "PNG"):
                files.append(tmp)
        if not files and not links:
            return False
        names = projects.add_refs(self.project, files) if files else []
        for url in links:
            projects.add_link(self.project, url)
        what = [f"`{n}`" for n in names] + [f"<{u}>" for u in links]
        self._add("note", "In refs/: " + ", ".join(what) + f". Tell {self._ai()} what to do with them.")
        return True

    def _on_mode(self, _checked):
        if self.project:
            projects.set_mode(self.project, "build" if self.build_btn.isChecked() else "teach")

    def _new_chat(self):
        if self.project:
            if backends.current() == "local":
                # each source keeps its own conversation: this one is the engine's file
                try:
                    if self.local_run is not None and self.local_run.running():
                        self.local_run.stop()     # or it would write the old conversation back
                    self._local_engine().clear(self.project)
                except Exception as e:
                    self._add("error", f"Could not start a new conversation: {e}")
                    return
            else:
                projects.set_session(self.project, None)
            self.entries = []
            self._add("note", "New conversation.")

    def _open_folder(self):
        if self.project:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.project))

    def _add_refs(self):
        if not self.project:
            return
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add references", os.path.expanduser("~"),
            "References (*.png *.jpg *.jpeg *.webp *.pdf *.dxf *.step *.stp *.txt *.md);;All files (*)")
        if files:
            added = projects.add_refs(self.project, files)
            self._add("note", "In refs/: " + ", ".join(f"`{a}`" for a in added) +
                      f". Tell {self._ai()} what to do with them.")

    def _settings(self):
        from .settings_dialog import SettingsDialog
        if SettingsDialog(self).exec():
            self._show_source()
            self._fill_projects()     # also shows the conversation of the source chosen now
            self.check_setup()

    # ---- talking to the AI ---------------------------------------------------
    def _send_or_stop(self):
        if self._running():
            self._stop_asked = True
            self._stop()
        else:
            self.send()

    def send(self, text=None):
        if self._running():
            return
        typed = text is None
        text = (self.input.toPlainText() if typed else text).strip()
        if not text:
            return
        if not self.project:
            self._add("error", "Pick or create a project first.")
            return
        backend = backends.current()
        token = None
        if backend == "local":
            if not self.check_setup():
                return
        else:
            key_missing = backend == "apikey" and not backends.api_key()
            if (not self.claude or key_missing) and not self.check_setup():
                return
            try:
                token, _ = bridge.ensure()
            except Exception as e:
                self._add("error", f"FreeCAD bridge: {e}")
                return
        if typed:
            self.input.clear()
        self._add("user", text)
        self._last_prompt, self._retried = text, False
        self._stop_asked = False
        if backend == "local":
            self._start_local(text)
        else:
            self._start(text, token)

    def _start(self, text, token):
        # the source's own variables (key, endpoint) are added by the runner: backends.claude_env
        env = {"FREECAD_MCP_TOKEN": token,
               "CADPROMPT_FREECADCMD": paths.freecadcmd_string(),
               "FREECAD_MCP_VERSION": paths.FREECAD_MCP_VERSION}
        self._engine = self.run
        self._busy(True, f"{self._who(start=True)} is working…")
        self.run.start(self.claude, self.project, text, projects.get_session(self.project), env)

    def _start_local(self, text):
        """No Claude Code, no bridge, no session id: the engine keeps its own conversation."""
        try:
            run = self._local_run()
            self._engine = run
            self._busy(True, f"{self._who(start=True)} is working…")
            run.start(self.project, text, backends.local_config())
        except Exception as e:
            self._busy(False)
            self._add("error", f"The local model could not be started: {e}")

    def _on_tool(self, name, detail):
        self._add("tool", f"`{name}` {detail}".strip())
        self.status.setText(f"{self._who(start=True)} is working… ({name})")

    def _on_tool_error(self, name):
        self._add("error", f"`{name}` failed, {self._who()} is looking at why")

    def _on_finished(self, res, run=None):
        run = run or self._engine
        local = run is not self.run
        if run is self._engine:      # a run that was stopped for a newer one must not end "working…"
            self._busy(False)
        if res.get("stopped"):
            # stopped on purpose: by the Stop button, a project change or a new chat
            if self._stop_asked:
                self._add("note", "Stopped.")
            self._stop_asked = False
            self._sync_mode()
            return
        self._stop_asked = False
        if not local:
            sid = res.get("session_id")
            if sid:
                projects.set_session(self.project, sid)
        self._sync_mode()
        seen = set()
        for d in res.get("permission_denials") or []:
            name = runner._short_tool(d.get("tool_name", "tool"))
            if name in seen:
                continue
            seen.add(name)
            if name in runner.WEB_TOOLS:
                self._add("error", f"`{name}` was blocked: web access is off. "
                                   "Turn it on in Settings, then ask again.")
            else:
                self._add("error", f"`{name}` was blocked by this project's permission rules.")
        if not res.get("is_error"):
            return
        if local:
            self._add("error", str(res.get("result") or "The local model stopped without an answer."))
            return
        msg = str(res.get("result") or res.get("subtype") or "Claude Code stopped.")
        if (not self._retried and projects.get_session(self.project)
                and "conversation" in msg.lower() and "found" in msg.lower()):
            projects.set_session(self.project, None)     # the old conversation is gone: start fresh
            self._retried = True
            self._add("note", "The earlier conversation could not be found; starting a new one.")
            self._start(self._last_prompt, bridge.ensure()[0])
            return
        if "login" in msg.lower() or "authenticat" in msg.lower():
            if backends.current() == "apikey":
                msg += "\n\nCheck the API key in Settings."
            else:
                msg += "\n\nSign in: open a terminal, run `claude`, and sign in with your Claude account."
        self._add("error", msg)


_dock = None


def _make_dock():
    """True when the dock was made by this call (it is on screen then)."""
    global _dock
    if _dock is not None:
        return False
    mw = Gui.getMainWindow()
    _dock = QtWidgets.QDockWidget("CADprompt", mw)
    _dock.setObjectName("CADpromptDock")
    _dock.setWidget(Panel(_dock))
    mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
    _dock.show()
    return True


def show():
    """Open the chat dock, or bring it back when it was closed. Never hides it."""
    if not _make_dock():
        _dock.show()
        _dock.raise_()


def toggle():
    if _make_dock():
        return
    _dock.setVisible(not _dock.isVisible())
    if _dock.isVisible():
        _dock.raise_()
