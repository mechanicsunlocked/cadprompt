"""Settings: projects folder, which model, where claude and uvx are."""

from PySide import QtWidgets

from . import paths


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CADprompt settings")
        p = paths.params()
        form = QtWidgets.QFormLayout(self)

        row = QtWidgets.QHBoxLayout()
        self.projects = QtWidgets.QLineEdit(p.GetString("ProjectsDir", ""))
        self.projects.setPlaceholderText(paths.projects_root())
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.projects, 1)
        row.addWidget(browse)
        form.addRow("Projects folder", row)

        self.account = QtWidgets.QRadioButton("Claude account (signed in to Claude Code)")
        self.custom = QtWidgets.QRadioButton("Other endpoint (local model, e.g. Ollama or LM Studio)")
        (self.custom if p.GetString("Backend", "account") == "custom" else self.account).setChecked(True)
        form.addRow("AI", self.account)
        form.addRow("", self.custom)

        self.model_override = QtWidgets.QLineEdit(p.GetString("ModelOverride", ""))
        self.model_override.setPlaceholderText("empty = Claude Code's default; or opus, sonnet, haiku")
        form.addRow("Claude model", self.model_override)

        self.url = QtWidgets.QLineEdit(p.GetString("BaseUrl", ""))
        self.url.setPlaceholderText("http://localhost:11434 (must speak the Anthropic Messages API)")
        form.addRow("Endpoint URL", self.url)
        self.model = QtWidgets.QLineEdit(p.GetString("Model", ""))
        self.model.setPlaceholderText("model name on that endpoint")
        form.addRow("Endpoint model", self.model)
        self.token = QtWidgets.QLineEdit(p.GetString("AuthToken", ""))
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.token.setPlaceholderText("usually empty for a local model")
        form.addRow("Endpoint token", self.token)

        self.claude = QtWidgets.QLineEdit(p.GetString("claudePath", ""))
        self.claude.setPlaceholderText(paths.find_tool("claude") or "not found")
        form.addRow("claude program", self.claude)
        self.uvx = QtWidgets.QLineEdit(p.GetString("uvxPath", ""))
        self.uvx.setPlaceholderText(paths.find_tool("uvx") or "not found")
        form.addRow("uvx program", self.uvx)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Projects folder", paths.projects_root())
        if d:
            self.projects.setText(d)

    def _save(self):
        p = paths.params()
        p.SetString("ProjectsDir", self.projects.text().strip())
        p.SetString("Backend", "custom" if self.custom.isChecked() else "account")
        p.SetString("ModelOverride", self.model_override.text().strip())
        p.SetString("BaseUrl", self.url.text().strip())
        p.SetString("Model", self.model.text().strip())
        p.SetString("AuthToken", self.token.text().strip())
        p.SetString("claudePath", self.claude.text().strip())
        p.SetString("uvxPath", self.uvx.text().strip())
        self.accept()
