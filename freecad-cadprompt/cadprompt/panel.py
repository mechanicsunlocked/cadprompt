"""The chat panel: a dock in FreeCAD that talks to the user's own Claude Code."""

import os

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtWidgets

from . import bridge, paths, projects, runner

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


class Panel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.run = runner.ClaudeRun(self)
        self.run.text.connect(lambda t: self._add("claude", t))
        self.run.tool.connect(self._on_tool)
        self.run.tool_error.connect(lambda n: self._add("error", f"`{n}` failed, Claude is looking at why"))
        self.run.finished.connect(self._on_finished)
        self.entries = []
        self.project = None
        self.claude = None
        self.claude_projects = None   # Claude Code's transcript folder, from auth status
        self.setAcceptDrops(True)
        self._last_prompt = None
        self._retried = False
        self._build_ui()
        self._fill_projects()
        QtCore.QTimer.singleShot(0, self.check_setup)

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
        self.build_btn.setToolTip("Claude builds the whole part")
        self.teach_btn = QtWidgets.QRadioButton("Teach")
        self.teach_btn.setToolTip("One feature at a time: which tool, where, and why")
        self.build_btn.setChecked(True)
        self.build_btn.toggled.connect(self._on_mode)
        row.addWidget(self.build_btn)
        row.addWidget(self.teach_btn)
        row.addStretch(1)
        newchat = QtWidgets.QToolButton(text="New chat")
        newchat.setToolTip("Start a fresh conversation in this project")
        newchat.clicked.connect(self._new_chat)
        row.addWidget(newchat)
        lay.addLayout(row)

        self.view = QtWidgets.QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view, 1)

        row = QtWidgets.QHBoxLayout()
        for label, tip, fn in (
            ("Add reference…", "Copy photos, PDFs or drawings into refs/ for Claude", self._add_refs),
            ("Check", "Review the model before it goes to the shop", lambda: self.send("/check")),
        ):
            b = QtWidgets.QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
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
            elif kind == "error":
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
    def check_setup(self):
        """Say what is missing, in words a student can follow. True when ready."""
        k = _os_key()
        if paths.in_flatpak():
            self._add("error", "This FreeCAD runs as a Flatpak and cannot start Claude Code from inside "
                               "its sandbox. Use the FreeCAD package or AppImage, or the terminal launcher.")
            return False
        if not paths.template_dir():
            self._add("error", "The project template is missing: install the whole cadprompt repository.")
            return False
        self.claude = paths.find_tool("claude")
        if not self.claude:
            self._add("error", "Claude Code is not installed. " + INSTALL_CLAUDE[k] +
                      " Then sign in once by running `claude` and restart FreeCAD.")
            return False
        if not paths.find_tool("uvx"):
            self._add("error", "uv is not installed (it runs the FreeCAD bridge). " + INSTALL_UV[k] +
                      " Then restart FreeCAD.")
            return False
        try:
            _, msg = bridge.ensure()
            if "expected" in msg:
                self._add("note", msg)
        except LookupError:
            ans = QtWidgets.QMessageBox.question(
                self, "CADprompt",
                f"The FreeCAD MCP bridge (freecad-mcp {paths.FREECAD_MCP_VERSION}) is not installed.\n\n"
                "Download it from GitHub (github.com/neka-nat/freecad-mcp) into FreeCAD's Mod folder now?")
            if ans != QtWidgets.QMessageBox.Yes:
                self._add("error", "Without the bridge Claude cannot reach FreeCAD.")
                return False
            try:
                self._add("note", bridge.install())
                bridge.ensure()
            except Exception as e:
                self._add("error", f"Could not install the bridge: {e}")
                return False
        except Exception as e:
            self._add("error", f"The FreeCAD bridge did not start: {e}")
            return False
        if not runner.backend_env():
            runner.auth_status(self.claude, self._on_auth, self)
        return True

    def _on_auth(self, st):
        if st and st.get("projectsDirectory"):
            self.claude_projects = st["projectsDirectory"]
            if len(self.entries) <= 1:
                self._load_history()
        if st is None:
            self._add("note", "Could not ask Claude Code whether you are signed in; trying anyway.")
        elif not st.get("loggedIn"):
            self._add("error", "Claude Code is not signed in. Open a terminal, run `claude`, sign in "
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
        if self.run.running():
            self.run.stop()
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
        sid = projects.get_session(self.project) if self.project else None
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
        self._add("note", "In refs/: " + ", ".join(what) + ". Tell Claude what to do with them.")
        return True

    def _on_mode(self, _checked):
        if self.project:
            projects.set_mode(self.project, "build" if self.build_btn.isChecked() else "teach")

    def _new_chat(self):
        if self.project:
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
                      ". Tell Claude what to do with them.")

    def _settings(self):
        from .settings_dialog import SettingsDialog
        if SettingsDialog(self).exec():
            self._fill_projects()

    # ---- talking to Claude --------------------------------------------------
    def _send_or_stop(self):
        if self.run.running():
            self.run.stop()
        else:
            self.send()

    def send(self, text=None):
        if self.run.running():
            return
        typed = text is None
        text = (self.input.toPlainText() if typed else text).strip()
        if not text:
            return
        if not self.project:
            self._add("error", "Pick or create a project first.")
            return
        if not self.claude and not self.check_setup():
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
        self._start(text, token)

    def _start(self, text, token):
        env = {"FREECAD_MCP_TOKEN": token,
               "CADPROMPT_FREECADCMD": paths.freecadcmd_string(),
               "FREECAD_MCP_VERSION": paths.FREECAD_MCP_VERSION}
        env.update(runner.backend_env())
        self._busy(True, "Claude is working…")
        self.run.start(self.claude, self.project, text, projects.get_session(self.project), env)

    def _on_tool(self, name, detail):
        self._add("tool", f"`{name}` {detail}".strip())
        self.status.setText(f"Claude is working… ({name})")

    def _on_finished(self, res):
        self._busy(False)
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
        msg = str(res.get("result") or res.get("subtype") or "Claude Code stopped.")
        if (not self._retried and projects.get_session(self.project)
                and "conversation" in msg.lower() and "found" in msg.lower()):
            projects.set_session(self.project, None)     # the old conversation is gone: start fresh
            self._retried = True
            self._add("note", "The earlier conversation could not be found; starting a new one.")
            self._start(self._last_prompt, bridge.ensure()[0])
            return
        if "login" in msg.lower() or "authenticat" in msg.lower():
            msg += "\n\nSign in: open a terminal, run `claude`, and sign in with your Claude account."
        self._add("error", msg)


_dock = None


def toggle():
    global _dock
    mw = Gui.getMainWindow()
    if _dock is None:
        _dock = QtWidgets.QDockWidget("CADprompt", mw)
        _dock.setObjectName("CADpromptDock")
        _dock.setWidget(Panel(_dock))
        mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
        _dock.show()
        return
    _dock.setVisible(not _dock.isVisible())
    if _dock.isVisible():
        _dock.raise_()
