"""Settings: projects folder, which AI does the work, and what that AI needs."""

import threading
import time
import urllib.error

from PySide import QtCore, QtWidgets

from . import backends, paths

# Linux and macOS: the key file has mode 0600. Windows has no such modes; there the
# folder of the user's own profile keeps other users out (backends.py).
KEY_PLACE = "a file only you can read" if backends.POSIX else "a file in your own user folder"
HINTS = {
    "account": "Uses your Claude subscription. Sign in once: run `claude` in a terminal.",
    "apikey": "Uses your key from console.anthropic.com; Anthropic bills what you use. "
              "Nothing else to install. Your messages, the images you send with them and what "
              "the tools report go to Anthropic. The key is kept in %s, not in FreeCAD's "
              "settings." % KEY_PLACE,
    "local": "A model on this computer or another server that speaks the OpenAI API. "
             "Nothing else to install: start the server, press Find models, pick one.",
    "custom": "Claude Code, sent to a server that speaks the Anthropic Messages API.",
}


def _why(exc, kind="openai"):
    """A failed 'Find models' in words a student can follow."""
    if kind == "anthropic":
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code in (401, 403):
                return "Anthropic did not accept the key."
            return f"Anthropic answered with error {exc.code}. Try again later."
        if isinstance(exc, ValueError):
            return "The answer of Anthropic was not a list of models. Try again later."
        return "No answer from Anthropic. Check the internet connection of this computer."
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "The server did not accept the key."
        return f"The server answered with error {exc.code}. Check the address (it usually ends in /v1)."
    if isinstance(exc, ValueError):
        return "That address did not answer with a list of models. Check it (it usually ends in /v1)."
    return "No answer from the server. Is it running, and is the address right?"


def _model_box(text, hint):
    """An editable list of models: the user can also type a name the list does not have."""
    box = QtWidgets.QComboBox()
    box.setEditable(True)
    box.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
    box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(24)
    box.setEditText(text)
    box.lineEdit().setPlaceholderText(hint)
    return box


class _KeyRow(QtWidgets.QWidget):
    """A key field that never shows a saved key: 'saved', a new one, or Remove."""

    def __init__(self, saved, empty_hint, parent=None):
        super().__init__(parent)
        self.remove = False
        self._saved = saved
        self._empty_hint = empty_hint
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QtWidgets.QLineEdit()
        self.edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.edit.setPlaceholderText("saved" if saved else empty_hint)
        self.edit.textEdited.connect(self._typed)
        self.drop = QtWidgets.QPushButton("Remove")
        self.drop.setToolTip("Delete the saved key when you press OK")
        self.drop.setEnabled(bool(saved))
        self.drop.clicked.connect(self._drop)
        row.addWidget(self.edit, 1)
        row.addWidget(self.drop)

    def _typed(self, _text):
        self.remove = False

    def _drop(self):
        self.remove = True
        self.edit.clear()
        self.edit.setPlaceholderText(self._empty_hint)
        self.drop.setEnabled(False)

    def show_saved(self, fits):
        """Say 'saved' only while the saved key is for the address in the dialog."""
        saved = self._saved and fits and not self.remove
        self.edit.setPlaceholderText("saved" if saved else self._empty_hint)
        self.drop.setEnabled(saved)

    def typed(self):
        return self.edit.text().strip()

    def save(self, setter):
        if self.typed():
            setter(self.typed())
        elif self.remove:
            setter("")


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CADprompt settings")
        p = paths.params()
        lay = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()
        row = QtWidgets.QHBoxLayout()
        self.projects = QtWidgets.QLineEdit(p.GetString("ProjectsDir", ""))
        self.projects.setPlaceholderText(paths.projects_root())
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.projects, 1)
        row.addWidget(browse)
        form.addRow("Projects folder", row)
        lay.addLayout(form)

        # ---- which AI ----
        ai = QtWidgets.QGroupBox("AI")
        box = QtWidgets.QVBoxLayout(ai)
        self.account = QtWidgets.QRadioButton("Claude account (signed in to Claude Code)")
        self.apikey = QtWidgets.QRadioButton("Anthropic API key")
        self.local = QtWidgets.QRadioButton("Local model or other server (LM Studio, Ollama, OpenAI-compatible)")
        self.custom = QtWidgets.QRadioButton("Claude Code with another endpoint")
        self.radios = {"account": self.account, "apikey": self.apikey,
                       "local": self.local, "custom": self.custom}
        for b in (self.account, self.apikey, self.local):
            box.addWidget(b)
        advanced = QtWidgets.QLabel("<small>Advanced</small>")
        advanced.setEnabled(False)       # greyed like a hint, in the theme's own colour
        box.addWidget(advanced)
        box.addWidget(self.custom)
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        box.addWidget(self.hint)

        # Anthropic API key
        self.apikey_box = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(self.apikey_box)
        f.setContentsMargins(0, 0, 0, 0)
        self.api_key = _KeyRow(bool(backends.api_key()), "sk-ant-…")
        f.addRow("API key", self.api_key)
        row = QtWidgets.QHBoxLayout()
        self.api_model = _model_box("", backends.API_MODEL)
        self.api_model.addItems(backends.API_MODELS)
        self.api_model.setEditText(backends.api_model())
        self.api_find = QtWidgets.QPushButton("Find models")
        self.api_find.setToolTip("Ask Anthropic which models your key can use. This sends the key to Anthropic.")
        self.api_find.clicked.connect(lambda: self.find_models("apikey"))
        row.addWidget(self.api_model, 1)
        row.addWidget(self.api_find)
        f.addRow("Model", row)
        self.api_find_msg = QtWidgets.QLabel("")
        self.api_find_msg.setWordWrap(True)
        f.addRow("", self.api_find_msg)
        box.addWidget(self.apikey_box)

        # local or other OpenAI-compatible server
        self.local_box = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(self.local_box)
        f.setContentsMargins(0, 0, 0, 0)
        self.local_url = QtWidgets.QLineEdit(p.GetString("LocalUrl", backends.LOCAL_URL))
        self.local_url.setPlaceholderText(backends.LOCAL_URL + "  (Ollama: http://127.0.0.1:11434/v1)")
        f.addRow("Server address", self.local_url)
        row = QtWidgets.QHBoxLayout()
        self.local_model = _model_box(p.GetString("LocalModel", ""), "model name on that server")
        self.find = QtWidgets.QPushButton("Find models")
        self.find.setToolTip("Ask the server which models it has")
        self.find.clicked.connect(lambda: self.find_models("local"))
        row.addWidget(self.local_model, 1)
        row.addWidget(self.find)
        f.addRow("Model", row)
        self.local_key = _KeyRow(bool(backends.local_key()), "empty for a server on this computer")
        self.local_url.textChanged.connect(lambda _text: self.local_key.show_saved(self._key_fits()))
        f.addRow("Key", self.local_key)
        self.find_msg = QtWidgets.QLabel("")
        self.find_msg.setWordWrap(True)
        f.addRow("", self.find_msg)
        box.addWidget(self.local_box)

        # Claude Code on another endpoint
        self.custom_box = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(self.custom_box)
        f.setContentsMargins(0, 0, 0, 0)
        self.url = QtWidgets.QLineEdit(p.GetString("BaseUrl", ""))
        self.url.setPlaceholderText("http://localhost:11434 (must speak the Anthropic Messages API)")
        f.addRow("Endpoint URL", self.url)
        self.model = QtWidgets.QLineEdit(p.GetString("Model", ""))
        self.model.setPlaceholderText("model name on that endpoint")
        f.addRow("Endpoint model", self.model)
        self.token = QtWidgets.QLineEdit(p.GetString("AuthToken", ""))
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.token.setPlaceholderText("usually empty for a local model")
        f.addRow("Endpoint token", self.token)
        box.addWidget(self.custom_box)
        lay.addWidget(ai)

        # ---- only for the sources that run Claude Code ----
        self.claude_box = QtWidgets.QGroupBox("Claude Code")
        self.claude_form = f = QtWidgets.QFormLayout(self.claude_box)
        self.web = QtWidgets.QCheckBox("Allow web search and reading web pages")
        self.web.setToolTip("Claude may look up standards, catalogues and datasheets. Text on web pages "
                            "is treated as data, never as instructions.")
        self.web.setChecked(p.GetBool("AllowWeb", False))
        f.addRow("Web", self.web)
        self.model_override = QtWidgets.QLineEdit(p.GetString("ModelOverride", ""))
        self.model_override.setPlaceholderText("empty = Claude Code's default; or opus, sonnet, haiku")
        self.model_label = QtWidgets.QLabel("Claude model")
        f.addRow(self.model_label, self.model_override)
        self.claude = QtWidgets.QLineEdit(p.GetString("claudePath", ""))
        self.claude.setPlaceholderText(paths.find_tool("claude") or "not found")
        f.addRow("claude program", self.claude)
        self.uvx = QtWidgets.QLineEdit(p.GetString("uvxPath", ""))
        self.uvx.setPlaceholderText(paths.find_tool("uvx") or "not found")
        f.addRow("uvx program", self.uvx)
        lay.addWidget(self.claude_box)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        # "Find models" asks in a thread; this timer picks the answer up in the GUI thread
        self._asking = None
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._find_poll)

        self.radios[backends.current()].setChecked(True)
        for b in self.radios.values():
            b.toggled.connect(self._show_source)
        self._show_source()

    def backend(self):
        for name, b in self.radios.items():
            if b.isChecked():
                return name
        return "account"

    def _show_source(self, *_):
        """Show only what the chosen source needs."""
        b = self.backend()
        self.hint.setText(HINTS[b])
        self.apikey_box.setVisible(b == "apikey")
        self.local_box.setVisible(b == "local")
        self.custom_box.setVisible(b == "custom")
        self.claude_box.setVisible(backends.uses_claude_code(b))
        # another endpoint has its own model field: the Claude model names mean nothing there
        for w in (self.model_label, self.model_override):
            w.setVisible(b == "account")
        self.adjustSize()

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Projects folder", paths.projects_root())
        if d:
            self.projects.setText(d)

    def _key_fits(self):
        """The saved key is for the saved address. It must not go to another server."""
        return backends.same_server(self.local_url.text(),
                                    paths.params().GetString("LocalUrl", backends.LOCAL_URL))

    # ---- Find models --------------------------------------------------------
    def find_models(self, source="local"):
        """Ask the service of a source for its models. Only on the user's click:
        this is a request to that service, with the key when there is one."""
        if self._asking:
            return
        if source == "apikey":
            kind, url, box, button, msg = "anthropic", backends.ANTHROPIC_URL, self.api_model, self.api_find, \
                self.api_find_msg
            key = self.api_key.typed()
            if not key and not self.api_key.remove:
                key = backends.api_key()
            if not key:
                msg.setText("Enter your API key first.")
                return
        else:
            kind, url, box, button, msg = "openai", self.local_url.text().strip(), self.local_model, self.find, \
                self.find_msg
            if not url:
                msg.setText("Enter the server address first, for example " + backends.LOCAL_URL)
                return
            if not url.lower().startswith(("http://", "https://")):
                msg.setText("Start the address with http:// or https://, for example " + backends.LOCAL_URL)
                return
            key = self.local_key.typed()
            if not key and not self.local_key.remove and self._key_fits():
                key = backends.local_key()
        if not backends.may_contact(kind, url):
            msg.setText("Nothing was sent: CADprompt is not allowed to contact this service.")
            return
        answer = {}

        def ask():
            try:
                answer["models"] = backends.list_models(url, key, timeout=5, kind=kind)
            except Exception as e:
                answer["error"] = e

        # the deadline is ours: a server that answers one byte at a time never trips the socket timeout
        self._asking = (answer, time.monotonic() + 6, kind, box, button, msg)
        button.setEnabled(False)
        msg.setText("Asking Anthropic…" if kind == "anthropic" else "Asking the server…")
        threading.Thread(target=ask, daemon=True).start()
        self._poll.start()

    def _find_poll(self):
        answer, deadline, kind, box, button, msg = self._asking
        if not answer and time.monotonic() < deadline:
            return
        self._poll.stop()
        self._asking = None
        button.setEnabled(True)
        if "models" not in answer:
            msg.setText(_why(answer.get("error"), kind))
            return
        models = answer["models"]
        if not models:
            msg.setText("Anthropic answered, but your key can use no model." if kind == "anthropic" else
                        "The server answered, but it has no models. Load one there first.")
            return
        keep = box.currentText().strip()
        box.clear()
        box.addItems(models)
        if keep in models:
            box.setCurrentIndex(models.index(keep))
        elif keep:
            box.setEditText(keep)
        n = len(models)
        msg.setText(f"Found {n} model{'' if n == 1 else 's'}. Pick one from the list.")

    def done(self, result):
        self._poll.stop()        # a late answer has nowhere to go once the dialog is closed
        super().done(result)

    def _save(self):
        if not self._key_fits() and not self.local_key.typed():
            self.local_key.remove = True     # new server, no new key: the old key does not go there
        try:
            self.api_key.save(backends.set_api_key)
            self.local_key.save(backends.set_local_key)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "CADprompt", f"Could not save the key: {e}")
            return
        p = paths.params()
        p.SetString("ProjectsDir", self.projects.text().strip())
        p.SetString("Backend", self.backend())
        p.SetString("LocalUrl", self.local_url.text().strip())
        p.SetString("LocalModel", self.local_model.currentText().strip())
        p.SetString("ApiModel", self.api_model.currentText().strip())
        p.SetBool("AllowWeb", self.web.isChecked())
        p.SetString("ModelOverride", self.model_override.text().strip())
        p.SetString("BaseUrl", self.url.text().strip())
        p.SetString("Model", self.model.text().strip())
        p.SetString("AuthToken", self.token.text().strip())
        p.SetString("claudePath", self.claude.text().strip())
        p.SetString("uvxPath", self.uvx.text().strip())
        self.accept()
