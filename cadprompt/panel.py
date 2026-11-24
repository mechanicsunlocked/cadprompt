"""The chat panel: a dock in FreeCAD where the user says what to build.

Who does the work is chosen in Settings (backends.py): Claude Code with the
user's account or another endpoint, or the built-in engine with a local model
or an Anthropic API key. The panel looks and works the same for all of them.

Claude Code reaches FreeCAD through the bridge (bridge.py), a server inside
this process. The panel starts it right before it starts Claude Code, and
stops it when it is closed or another kind of source is chosen.

Two questions to the user live here. Before data leaves the computer:
_consent(), which asks backends.may_contact(). Before a step of the AI runs:
ask_approval(), which approval.request() calls for both engines. A new
installation has no source: the panel shows three choices and contacts nothing.
"""

import html
import os

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtWidgets

from . import approval, ask_ui, backends, bridge, consent, paths, projects, runner

NEW = "+ New project…"
NO_SOURCE = "Choose the AI that does the work first: press one of the three buttons above."
ASKS_FIRST = ("CADprompt did not start Claude Code yet, also not to see whether you are signed in. "
              "It asks you first, with your first message.")
WAITING = "Waiting for you: Run or Skip."
NOT_WAITED = "%s did not wait for your answer. This step was not run."
AUTO_MARK = "runs without asking"
AUTO_OFF = "From now on CADprompt asks before it runs the AI's code."
FORGOT = "Claude was stopped: you took the permission for this service back."
IMAGES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")   # references that can go with a message

INSTALL_CLAUDE = {
    "win": "In PowerShell: `irm https://claude.ai/install.ps1 | iex`",
    "mac": "In Terminal: `curl -fsSL https://claude.ai/install.sh | bash`",
    "linux": "In a terminal: `curl -fsSL https://claude.ai/install.sh | bash`",
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
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def show_text(self, text):
        # the name of a model comes from the list of a server: it is shown as plain text
        self.full = text
        self.setToolTip("<qt>%s</qt>" % html.escape("The AI that does the work: " + text
                                                    + ". Change it in Settings."))
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        self.setText(self.fontMetrics().elidedText(self.full, QtCore.Qt.ElideRight, self.width()))


class _View(QtWidgets.QTextBrowser):
    """The chat. Its text comes from the AI and from files of the project, and Qt opens
    every file that such a text names as an image, a background or a style sheet. On
    Windows `file://host/share/x.png` is a login at that host. This view opens no file."""

    NO_IMAGE = "(image not shown)"
    _OBJECT = "\ufffc"         # the character that stands for an image in a Qt document

    def __init__(self, parent=None):
        super().__init__(parent)
        self._blank = QtGui.QImage(1, 1, QtGui.QImage.Format_ARGB32)
        self._blank.fill(0)

    def loadResource(self, kind, url):
        # never None: the document then opens the file itself
        return self._blank

    def setMarkdown(self, text):
        super().setMarkdown(text)
        # An image that stays is looked for again when it is painted, past loadResource
        # (`x@2x.png` beside it, on a screen with a scale factor): it becomes words.
        doc = self.document()
        cur = doc.find(self._OBJECT)
        while not cur.isNull():
            if cur.charFormat().isImageFormat():
                cur.insertText(self.NO_IMAGE, QtGui.QTextCharFormat())
            cur = doc.find(self._OBJECT, cur)


class Panel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.run = runner.ClaudeRun(self)     # Claude Code: account, other endpoint
        self.local_run = None                 # the built-in engine, made when first needed
        self._working = False                 # a request is under way (the button says Stop)
        self._engine = self.run               # the one that got the last message
        self._sent_to = backends.current()    # the source that got the last message
        self._images = []                     # image references added since the last message
        self._connect(self.run)
        self._check = 0                       # counts check_setup runs: late answers are dropped
        self._stop_asked = False
        self._closed = False                  # the dock is hidden: the bridge is not kept open
        self._project_notes = []              # what projects.migrate() says about this project
        self._migrated = False                # projects.migrate() ran for this project
        self._approval = None                 # "" while a step waits for Run or Skip, then the answer
        self._run_all = False                 # "Run all for this request": until this request ends
        self._sending = False                 # send() is asking the user: no second send meanwhile
        self._said_local = False              # "runs on this computer" was said in this panel
        self._open = []                       # the code entries that are shown in full
        self._alive = alive = {"dead": False}
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
        self._show_auto()
        approval.register(self)
        self.destroyed.connect(lambda *_: bridge.stop())
        # a question that still waits must not touch a panel that Qt deleted
        self.destroyed.connect(lambda *_, me=self: (alive.__setitem__("dead", True), approval.unregister(me)))
        QtCore.QTimer.singleShot(0, self.check_setup)

    # ---- the two engines ----------------------------------------------------
    def _connect(self, run):
        run.text.connect(lambda t: self._add("claude", t))
        run.tool.connect(self._on_tool)
        run.tool_error.connect(self._on_tool_error)
        run.tool_done.connect(lambda _name, run=run: self._on_tool_done(run))
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
        self._end_question()
        if self.run.running():
            bridge.idle()         # a tool call of Claude Code that still waits must not run after Stop
            self.run.stop()
        if self.local_run is not None and self.local_run.running():
            self.local_run.stop()

    # ---- the bridge: open only while Claude Code can need it ---------------------
    def _bridge_idle(self):
        """Close the bridge when the panel is closed, the source is not Claude Code, or the
        user took the yes for Claude Code back (Settings > Privacy)."""
        if not self.run.running() and (self._closed or not backends.uses_claude_code() or not runner.allowed()):
            bridge.stop()

    def _yes_taken_back(self):
        """Forget in Settings > Privacy, while the dialog is still open. Claude Code is a
        program of its own: it would send data until its request ends, so it is stopped now.
        The built-in engine needs nothing here: it asks before its next request."""
        if self.run.running() and not self._allowed(self._sent_to):
            self._add("note", FORGOT)
            self._stop()
        self._bridge_idle()

    def _bridge_note(self, name, detail):
        """What the bridge did by itself for a request of Claude Code (the backup copy)."""
        self._add("tool", f"`{name}` {detail}".strip())

    def hideEvent(self, e):
        super().hideEvent(e)
        self._closed = True
        self._bridge_idle()

    def showEvent(self, e):
        super().showEvent(e)
        self._closed = False

    @staticmethod
    def _name(backend, start=False):
        """What the AI of a source is called in messages: every source but the local one is Claude."""
        if not backend:
            return "The AI" if start else "the AI"
        if backend != "local":
            return "Claude"
        return "The local model" if start else "the local model"

    def _who(self, start=False):
        """Who is working on the message that was sent."""
        return self._name(self._sent_to, start)

    def _ai(self, start=False):
        """Who the next message goes to."""
        return self._name(backends.current(), start)

    def _show_source(self):
        """The label next to the mode buttons, and the tooltips that name the AI."""
        b = backends.current()
        p = paths.params()
        model = {"local": p.GetString("LocalModel", "").strip() or "no model yet",
                 "apikey": backends.api_model(),
                 "custom": p.GetString("Model", "").strip()}.get(b, p.GetString("ModelOverride", "").strip())
        self.source.show_text(backends.label(b) + (" · " + model if model and b else ""))
        self.chooser.setVisible(not b)       # a new installation: the user chooses first
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

        self.chooser = ask_ui.Chooser()
        self.chooser.chosen.connect(self._choose)
        self.chooser.hide()
        lay.addWidget(self.chooser)

        self.view = _View()
        self.view.setOpenLinks(False)        # "show" and "hide" of a piece of code are links of ours
        self.view.anchorClicked.connect(self._on_link)
        lay.addWidget(self.view, 1)

        # the step of the AI that waits for Run or Skip: under the chat, the model stays in view
        self.card = ask_ui.ApprovalCard()
        self.card.answered.connect(self._on_card)
        lay.addWidget(self.card)

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
        self.status.setTextFormat(QtCore.Qt.PlainText)     # it shows a tool name that the AI gave
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.auto_btn = QtWidgets.QToolButton(text=AUTO_MARK)
        self.auto_btn.setToolTip("The AI's code runs without a question (Settings). "
                                 "Click to turn that off: CADprompt then asks before each step.")
        self.auto_btn.clicked.connect(self._auto_off)
        row.addWidget(self.auto_btn)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send_or_stop)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

    def _add(self, kind, text):
        self.entries.append((kind, text))
        self._render()

    @staticmethod
    def _fence(text, least=1):
        """Backticks to put around `text` in Markdown: more than the text itself has in a row."""
        run = longest = 0
        for c in text:
            run = run + 1 if c == "`" else 0
            longest = max(longest, run)
        return "`" * max(least, longest + 1)

    def _code_part(self, i, kind, text):
        """A step that the user saw, or that ran without a question: one small line.
        Code of more than one line is folded; "show" opens it."""
        # Qt shows Markdown inside an HTML tag in the wrong order: the tag holds plain words only
        what = "ran" if kind == "code" else "skipped, not run"
        if "\n" not in text.strip():
            fence = self._fence(text)
            return f"<small>▸ {what}:</small> {fence} {text.strip()} {fence}"
        lines = text.count("\n") + 1
        if not any(e is self.entries[i] for e in self._open):
            return f"<small>▸ {what}: {lines} lines of code ·</small> [show](cadprompt:code/{i})"
        fence = self._fence(text, 3)
        return (f"<small>▾ {what}: {lines} lines of code ·</small> [hide](cadprompt:code/{i})\n\n"
                f"{fence}python\n{text}\n{fence}")

    def _on_link(self, url):
        if url.scheme() != "cadprompt":
            # a link in an answer: the browser of the system. Web links only: the text comes
            # from the AI, and a link to a file or a program must not open with a click.
            if url.scheme().lower() in ("http", "https"):
                QtGui.QDesktopServices.openUrl(url)
            return
        try:
            entry = self.entries[int(url.path().rsplit("/", 1)[-1])]
        except (ValueError, IndexError):
            return
        if any(e is entry for e in self._open):
            self._open = [e for e in self._open if e is not entry]
        else:
            self._open.append(entry)
        self._render(keep=True)

    def _render(self, keep=False):
        parts = []
        sb = self.view.verticalScrollBar()
        was = sb.value()
        self._open = [e for e in self._open if any(e is one for one in self.entries)]
        for i, (kind, text) in enumerate(self.entries):
            if kind in ("code", "skipped"):
                parts.append(self._code_part(i, kind, text))
            elif kind == "user":
                parts.append("> **You:** " + text.replace("\n", "\n> "))
            elif kind == "claude":
                parts.append(text)
            elif kind == "tool":
                parts.append(f"<small>▸</small> {text}")     # not the text in the tag: see _code_part
            elif kind in ("error", "setup"):
                parts.append(f"**⚠** {text}")
            else:
                parts.append(f"*{text}*")
        self.view.setMarkdown("\n\n".join(parts))
        sb.setValue(min(was, sb.maximum()) if keep else sb.maximum())

    def _busy(self, on, msg=""):
        self._working = on
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
        backend = backends.current()
        if not backend:
            # no source chosen: nothing is looked for, started or contacted (see the chooser)
            self._bridge_idle()
            return False
        if backend in backends.ENGINE:
            self._bridge_idle()
            return self._check_engine()
        return self._check_claude_code()

    def _check_engine(self):
        """The built-in engine needs no Claude Code and no bridge: a local model
        needs a server and a model, the API key source a key and a model."""
        if not paths.template_dir():
            self._setup("The project template is missing: install the whole cadprompt repository.")
            return False
        cfg = backends.engine_config()
        if cfg["kind"] == "anthropic":
            if not cfg["key"]:
                self._setup("No Anthropic API key is saved. Open Settings and paste your key.")
                return False
        elif not cfg["url"]:
            self._setup("No server address is set for the local model. Open Settings and enter it, "
                        f"for example `{backends.LOCAL_URL}` for LM Studio.")
            return False
        if not cfg["model"]:
            self._setup("No model is chosen yet. Open Settings, press Find models and pick one.")
            return False
        try:
            self._local_engine()
        except Exception as e:
            self._setup(f"The built-in engine of CADprompt did not load: {e}")
            return False
        return True

    def _check_claude_code(self):
        k = _os_key()
        if paths.in_flatpak():
            self._setup("This FreeCAD runs as a Flatpak and cannot start Claude Code from inside "
                        "its sandbox. Use the FreeCAD package or AppImage, or the terminal launcher, "
                        "or choose a local model or an API key in Settings.")
            return False
        if not paths.template_dir():
            self._setup("The project template is missing: install the whole cadprompt repository.")
            return False
        self.claude = paths.find_tool("claude")
        if not self.claude:
            self._setup("Claude Code is not installed. " + INSTALL_CLAUDE[k] +
                        " Then sign in once by running `claude` and restart FreeCAD.")
            return False
        self._show_project_notes()
        self._bridge_idle()
        if not self._allowed():
            # the program runs with a login found on this computer: not before the user's yes
            self._setup(ASKS_FIRST, note=True)
            return True
        if not runner.backend_env():
            runner.auth_status(self.claude, lambda st, n=self._check: self._on_auth(st, n), self)
        return True

    def _on_auth(self, st, check=None):
        if check is not None and check != self._check:
            return      # the settings changed while Claude Code was answering
        if st and st.get("projectsDirectory"):
            self.claude_projects = st["projectsDirectory"]
            if len([e for e in self.entries if not e[0].startswith("setup")]) <= 1:
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
        paths.save_params()
        self._project_notes, self._migrated = [], False      # see _migrate: not when a project is picked
        runner.allow_web_in_project(self.project)
        self._sync_mode()
        self.entries = []
        self._images = []             # they are in the refs/ of the project that was open
        self._add("note", f"Project **{name}**.")
        self._load_history()
        if open_model:
            model = projects.newest_model(self.project)
            if model:
                try:
                    projects.open_model(model)
                except Exception as e:
                    self._add("error", f"Could not open {os.path.basename(model)}: {e}")

    def _migrate(self):
        """Bring a project from an older template up to date, and say what was replaced.

        Only before the first message to Claude Code, not when a project is
        picked: the built-in engine reads none of these files, and the terminal
        launcher, which shares the projects folder, works with the old ones.
        """
        if self._migrated:
            return
        self._migrated = True
        replaced = []
        try:
            self._project_notes = projects.migrate(self.project, replaced)
        except Exception as e:
            self._project_notes = []
            App.Console.PrintWarning(f"CADprompt: could not bring the project up to date: {e}\n")
        if replaced:
            self._add("note", "This project came from an older CADprompt. These files were replaced by the "
                              "new ones, which name the FreeCAD tools of this version: %s. Each old file is "
                              "kept beside the new one as `<name>.old`."
                              % ", ".join("`%s`" % rel for rel in replaced))
        self._show_project_notes()

    def _show_project_notes(self):
        """Say what a file of the project that was left alone means for Claude Code."""
        shown = [t for k, t in self.entries if k == "setup-note"]
        for text in self._project_notes:
            if text not in shown:
                self._setup(text, note=True)

    def _load_history(self):
        """The earlier conversation of this project with the source chosen now."""
        if not self.project or not backends.current():
            return
        if not backends.uses_claude_code():
            try:
                old = self._local_engine().history(self.project)
            except Exception as e:
                App.Console.PrintWarning(f"CADprompt: could not read the conversation: {e}\n")
                return
        else:
            sid = projects.get_session(self.project)
            path = sid and runner.transcript_path(self.project, sid, self.claude_projects)
            if not path:
                return
            old = runner.read_history(path)
        if old:
            kept = [e for e in self.entries[1:] if e[0].startswith("setup")]
            self.entries = self.entries[:1] + old + [("note", "Continuing this conversation.")] + kept
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
        self._added(names, links)
        return True

    def _added(self, names, links=()):
        """Say what went into refs/. An image also goes with the next message to the
        built-in engine; Claude Code opens the files in refs/ itself."""
        what = [f"`{n}`" for n in names] + [f"<{u}>" for u in links]
        text = "In refs/: " + ", ".join(what) + "."
        images = [n for n in names if n.lower().endswith(IMAGES)]
        self._images += [os.path.join(self.project, "refs", n) for n in images]
        if images and backends.current() in backends.ENGINE:
            text += (" The image goes" if len(images) == 1 else " The images go") + \
                    f" to {self._ai()} with your next message."
        else:
            text += f" Tell {self._ai()} what to do with them."
        self._add("note", text)

    def _on_mode(self, _checked):
        if self.project:
            projects.set_mode(self.project, "build" if self.build_btn.isChecked() else "teach")

    def _new_chat(self):
        if self.project:
            # The request of the old conversation ends, for every source: a step of it that
            # still waits is not run, and a run that went on would ask again in the new chat
            # and write the old conversation back when it ends.
            self._stop()
            if backends.current() in backends.ENGINE:
                # Claude Code keeps its own conversation: this one is the engine's file
                try:
                    self._local_engine().clear(self.project)
                except Exception as e:
                    self._add("error", f"Could not start a new conversation: {e}")
                    return
            else:
                projects.set_session(self.project, None)
            self.entries = []
            self._images = []
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
            self._added(projects.add_refs(self.project, files))

    def _settings(self, choose=None):
        """Open Settings. `choose`: the source that the user picked in the chooser."""
        from .settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.forgot.connect(self._yes_taken_back)
        if choose:
            dlg.choose(choose)
        before = backends.service()
        if dlg.exec():
            self._show_source()
            self._show_auto()
            self._fill_projects()     # also shows the conversation of the source chosen now
            if self.check_setup() and backends.service() != before and not self._allowed():
                # the user just chose this service: ask now, so the sign-in can be checked.
                # Not for a service that was there before: a Forget or a "Not now" is not
                # answered with the same question at once; the next message asks.
                if self._consent(backends.current()) and backends.uses_claude_code():
                    self.check_setup()
        else:
            # Forget works at once, also with Cancel: _yes_taken_back stopped a request of
            # Claude Code that was running when the button was pressed
            self._bridge_idle()

    def _choose(self, name):
        self._settings(choose=name)

    # ---- the user's yes: before data leaves, before code runs ---------------------
    def _allowed(self, backend=None):
        """True when the service of a source needs no question any more. Asks nothing."""
        service = backends.service(backend)
        return bool(service) and (consent.needs_none(*service) or consent.allowed(*service))

    def _consent(self, backend):
        """May this source get data now? Asks the user the first time. Says what a no means."""
        service = backends.service(backend)
        if service and backends.may_contact(*service):
            if backend == "local" and consent.on_this_computer(service[1]) and not self._said_local:
                self._said_local = True
                self._add("note", consent.ON_THIS_COMPUTER)
            return True
        self._add("note", consent.NOT_NOW)
        return False

    def _show_auto(self):
        self.auto_btn.setVisible(approval.auto())

    def _auto_off(self):
        approval.set_auto(False)
        self._show_auto()
        self._add("note", AUTO_OFF)

    def asks(self):
        """approval.request() asks the panel whose request is running."""
        return self._running()

    def asking(self):
        """True while a step of the AI waits for Run or Skip."""
        return self._approval == ""

    def _on_card(self, word):
        if self.asking():
            self._approval = word

    def _end_question(self):
        """The request ends: a step that still waits is not run."""
        if self.asking():
            self._approval = "stop"

    def withdraw(self):
        """approval.withdraw(): the AI does not wait for the answer to its step any more."""
        if self.asking():
            self._approval = "gone"

    def ask_approval(self, tool_name, args, source):
        """Show a step of the AI and wait for the user. True: it may run, as it was shown.

        Called by approval.request() inside a slot of the engine or the bridge.
        The wait lets FreeCAD work, so Stop, a new chat and a closed panel all
        get through; each of them ends the question with a no.
        """
        text = approval.shown(tool_name, args)
        if approval.auto() or self._run_all:
            self._add("code", text)          # folded in the chat: the user can read what ran
            return True
        if self._approval is not None:
            return False                     # one question at a time
        who = self._name(source, start=True)
        title = {"run_python": f"{who} wants to run this code in FreeCAD:",
                 "make_weld": f"{who} wants to weld:"}.get(tool_name, f"{who} wants to make a drawing:")
        alive = self._alive
        self._approval = ""
        self.card.ask(title, text)
        dock = self.parent()
        if isinstance(dock, QtWidgets.QDockWidget) and not self.isVisible():
            dock.show()                      # closed or behind another tab while the AI worked:
            dock.raise_()                    # a question nobody sees would wait for ever
        # The focus stays where it is. After Send it is in the empty message box, and the
        # user can start the next message without a look at the screen: those keys, and
        # the Enter at their end, must not answer the question.
        self.status.setText(WAITING)
        in_time = ask_ui.wait_until(lambda: alive["dead"] or self._approval != "")
        if alive["dead"]:
            return False
        choice, self._approval = self._approval, None
        back = self.card.hasFocus() or any(w.hasFocus() for w in self.card.findChildren(QtWidgets.QWidget))
        self.card.hide()
        if back:
            self.input.setFocus()
        if not in_time or not choice:        # FreeCAD is closing: end the request, run nothing
            self._stop()
            return False
        if choice == "stop":
            return False
        if self._running():
            self.status.setText(f"{self._who(start=True)} is working… ({tool_name})")
        if choice == "gone":
            self._add("note", NOT_WAITED % self._who(start=True))
            return False
        if choice == "skip":
            self._add("skipped", text)
            return False
        if choice == "all":
            self._run_all = True
        self._add("code", text)
        return True

    def closeEvent(self, e):
        self._closed_by_user()
        super().closeEvent(e)

    def eventFilter(self, obj, e):
        # the dock was closed with its button (not: another tab is in front)
        if e.type() == QtCore.QEvent.Close and obj is self.parent():
            self._closed_by_user()
        return super().eventFilter(obj, e)

    def _closed_by_user(self):
        """Nobody can answer a question in a closed panel: the request ends."""
        if self.asking():
            self._stop_asked = True
            self._stop()

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
        if not backend:
            self._add("error", NO_SOURCE)
            return
        if self._sending:
            return                # the question of the last Send is still open
        engine = backend in backends.ENGINE
        where, images = None, []
        self._sending = True
        try:
            if engine:
                if not self.check_setup() or not self._consent(backend):
                    return
                images = self._take_images()
                if images is None:
                    return
            else:
                asked = not self._allowed(backend)
                if not self.claude and not self.check_setup():
                    return
                # the bridge, the sign-in check and Claude Code itself: nothing before the user's yes
                if not self._consent(backend):
                    return
                if asked and not self.check_setup():
                    return        # the yes is new: now the sign-in is looked at too
                self._migrate()   # a project made by an earlier version: its files name tools that are gone
                try:
                    where = bridge.ensure(self.project, backend, self._bridge_note)
                except Exception as e:
                    self._add("error", f"The FreeCAD tools for Claude could not be started: {e}")
                    return
        finally:
            self._sending = False
        self._run_all = False
        self._show_auto()
        self._images = []
        if typed:
            self.input.clear()
        self._add("user", text)
        self._last_prompt, self._retried = text, False
        self._stop_asked = False
        self._sent_to = backend
        if engine:
            self._start_local(text, backend, images)
        else:
            self._start(text, *where)

    def _take_images(self):
        """The images that go with this message, as paths in the project.
        None when one of them is no image: the message is not sent then."""
        files = [f for f in self._images if os.path.isfile(f)]
        left = len(files) - self._local_engine().MAX_IMAGES
        if left > 0:
            files = files[left:]
            self._add("note", f"Only the newest {len(files)} images go with a message. "
                              "The others stay in refs/.")
        try:
            return self._local_engine().project_images(self.project, files)
        except ValueError as e:
            bad = getattr(e, "path", None)
            self._images = [f for f in files if f != bad]
            self._add("error", f"{e} Press Send again to send your message without it.")
            return None

    def _start(self, text, url, token):
        """`url` and `token`: where Claude Code finds the FreeCAD tools (bridge.ensure)."""
        # the source's own variables (endpoint) are added by the runner: backends.claude_env
        env = runner.mcp_env(url, token)
        # /build and /teach: the panel writes the mode file, as the built-in engine does.
        # Claude Code may not change a file of the project without a question.
        word = text.split(None, 1)[0] if text.strip() else ""
        if word in ("/build", "/teach"):
            projects.set_mode(self.project, word[1:])
            self._sync_mode()
        self._engine = self.run
        self._busy(True, f"{self._who(start=True)} is working…")
        self.run.start(self.claude, self.project, text, projects.get_session(self.project), env)

    def _start_local(self, text, backend, images=()):
        """No Claude Code, no bridge, no session id: the engine keeps its own conversation."""
        try:
            run = self._local_run()
            self._engine = run
            self._busy(True, f"{self._who(start=True)} is working…")
            if images:
                self._add("note", self._local_engine().images_note(len(images)))
            run.start(self.project, text, backends.engine_config(backend), images=list(images))
        except Exception as e:
            self._busy(False)
            self._add("error", f"{self._who(start=True)} could not be started: {e}")

    def _on_tool(self, name, detail):
        self._add("tool", f"`{name}` {detail}".strip())
        self.status.setText(f"{self._who(start=True)} is working… ({name})")

    def _on_tool_done(self, run):
        """The result of a tool went back to the AI. It reads the result and decides the next
        step now, and that can take a minute: the tool name in the status would say that
        FreeCAD still works on the tool."""
        if run is self._engine and self._working and not self.asking():
            self.status.setText(f"{self._who(start=True)} is thinking…")

    def _on_tool_error(self, name):
        self._add("error", f"`{name}` failed, {self._who()} is looking at why")

    def _on_finished(self, res, run=None):
        run = run or self._engine
        local = run is not self.run
        if run is self._engine:
            self._end_question()     # a step that still waits belongs to a request that is over
            self._run_all = False
        if not local:
            bridge.idle()            # Claude Code is gone: a call of it that still waits is dropped
            self._bridge_idle()
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
            elif name in runner.FILE_TOOLS:
                # see ClaudeRun.start. The CLAUDE.md of the template tells Claude Code the same.
                self._add("error", f"`{name}` was blocked: Claude Code cannot write files itself in this panel. "
                                   "Ask it to save the file with run_python: you then see the code and press Run.")
            else:
                self._add("error", f"`{name}` was blocked: Claude Code must ask you before it uses this tool, "
                                   "and it cannot ask in this panel.")
        if not res.get("is_error"):
            return
        if local:
            self._add("error", str(res.get("result") or f"{self._who(start=True)} stopped without an answer."))
            if res.get("no_vision") and self._last_prompt and not self.input.toPlainText().strip():
                # the message is back in the input: Send now sends it without the images
                self.input.setPlainText(self._last_prompt)
            return
        msg = str(res.get("result") or res.get("subtype") or "Claude Code stopped.")
        if (not self._retried and projects.get_session(self.project)
                and "conversation" in msg.lower() and "found" in msg.lower()):
            projects.set_session(self.project, None)     # the old conversation is gone: start fresh
            self._retried = True
            self._add("note", "The earlier conversation could not be found; starting a new one.")
            try:
                where = bridge.ensure(self.project, self._sent_to, self._bridge_note)
            except Exception as e:
                self._add("error", f"The FreeCAD tools for Claude could not be started: {e}")
                return
            self._start(self._last_prompt, *where)
            return
        if "login" in msg.lower() or "authenticat" in msg.lower():
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
    _dock.installEventFilter(_dock.widget())      # the panel hears when its dock is closed
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
    if _dock.isVisible():
        _dock.close()             # as the button of the dock does: a question that waits ends
    else:
        _dock.show()
        _dock.raise_()
