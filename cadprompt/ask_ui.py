# SPDX-License-Identifier: MIT
"""The questions that CADprompt asks the user, as Qt widgets.

- confirm(): a small window with a question and two buttons. The consent
  question (consent.py) and the warning for "run without asking" use it.
- ApprovalCard: the code of the AI with Run and Skip. It sits in the chat
  panel, not in a window of its own, so the model stays in view.
- Chooser: the three choices of a new installation.

A question waits with wait_until(), which lets FreeCAD's events through. No
event loop of our own is started (QDialog.exec, QEventLoop.exec): on macOS
such a loop did not always come back (tests/README.md, "How to add a test").
"""

import time
import unicodedata

import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtWidgets

from . import consent


def wait_until(done, step=0.01):
    """Let FreeCAD work until done() is true. False when FreeCAD is closing first.

    Call it in the GUI thread. The caller is inside a Qt slot most of the
    time, so everything can happen meanwhile: Stop, a closed panel, another
    document. The caller looks again afterwards.
    """
    app = QtWidgets.QApplication.instance()
    window = Gui.getMainWindow() if app is not None else None
    on_screen = window is not None and window.isVisible()
    while not done():
        if app is None or QtCore.QCoreApplication.closingDown():
            return False
        if on_screen and not window.isVisible():
            return False         # the main window was closed: FreeCAD ends when we return
        QtWidgets.QApplication.processEvents()
        time.sleep(step)
    return True


# ---- a question in a small window ------------------------------------------------
class Question(QtWidgets.QDialog):
    """A headline, a text and two buttons. `answer` is None until the user chose."""

    def __init__(self, parent, headline, text, yes, no):
        super().__init__(parent)
        self.setWindowTitle("CADprompt")
        self.answer = None
        lay = QtWidgets.QVBoxLayout(self)
        self.headline = QtWidgets.QLabel(headline)
        font = self.headline.font()
        font.setBold(True)
        self.headline.setFont(font)
        self.headline.setTextFormat(QtCore.Qt.PlainText)     # it names a host that the user typed
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)
        self.text = QtWidgets.QLabel(text)
        self.text.setWordWrap(True)
        self.text.setTextFormat(QtCore.Qt.PlainText)
        self.text.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.text.setMinimumWidth(460)
        lay.addWidget(self.text)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.yes_btn = QtWidgets.QPushButton(yes)
        self.no_btn = QtWidgets.QPushButton(no)
        self.yes_btn.setAutoDefault(False)
        self.no_btn.setDefault(True)         # Enter never says yes by accident
        self.yes_btn.clicked.connect(self.accept)
        self.no_btn.clicked.connect(self.reject)
        row.addWidget(self.yes_btn)
        row.addWidget(self.no_btn)
        lay.addLayout(row)
        self.no_btn.setFocus()               # and Space does not either

    def done(self, result):
        self.answer = result == QtWidgets.QDialog.Accepted
        super().done(result)


def _window():
    return QtWidgets.QApplication.activeModalWidget() or Gui.getMainWindow()


def confirm(parent, headline, text, yes, no):
    """Ask in a window over `parent` (or FreeCAD's window). True only for a click on `yes`."""
    dlg = Question(parent or _window(), headline, text, yes, no)
    dlg.setModal(True)
    dlg.show()
    wait_until(lambda: dlg.answer is not None)
    answer = dlg.answer is True
    try:
        dlg.hide()
        dlg.deleteLater()
    except RuntimeError:         # its parent window is gone, and the question with it
        pass
    return answer


def ask_consent(kind, url):
    """The question of consent.may_contact(): may data go to this service?"""
    headline, text = consent.question(kind, url)
    return confirm(None, headline, text, consent.ALLOW, consent.LATER)


consent.asker = ask_consent


# ---- the code of the AI, with Run and Skip -----------------------------------------
def hidden_characters(text):
    """True when the text has characters that a text box does not show (they can hide code)."""
    return any(unicodedata.category(c) in ("Cc", "Cf") and c not in "\n\t" for c in text)


class ApprovalCard(QtWidgets.QFrame):
    """One step of the AI that waits for the user: the text, Run, Skip, Run all."""

    answered = QtCore.Signal(str)       # "run", "skip" or "all"
    # Enter and Esc work only after a click into the card (it never takes the focus itself),
    # and do nothing in its first moment or as the repeat of a key that is held down: a key
    # that was on its way to something else must not run code.
    KEY_DELAY = 0.4
    ROWS = 12                           # lines of code in view; the rest scrolls

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._since = 0.0
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.title = QtWidgets.QLabel("")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.code = QtWidgets.QPlainTextEdit()
        self.code.setReadOnly(True)
        self.code.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.code.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.code.setTabChangesFocus(True)
        lay.addWidget(self.code)
        self.more = QtWidgets.QLabel("")     # says so when a part of the text is outside the view
        self.more.setWordWrap(True)
        self.more.hide()
        lay.addWidget(self.more)
        for bar in (self.code.verticalScrollBar(), self.code.horizontalScrollBar()):
            bar.rangeChanged.connect(self._tell_more)    # also when the user makes the panel smaller
        row = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("Run")
        self.run_btn.setToolTip("Run exactly this, one time (Enter)")
        self.skip_btn = QtWidgets.QPushButton("Skip")
        self.skip_btn.setToolTip("Do not run it. The AI is told that you did not approve this step (Esc)")
        row.addWidget(self.run_btn)
        row.addWidget(self.skip_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.all_btn = QtWidgets.QPushButton("Run all for this request")
        self.all_btn.setToolTip("Run this and the next steps of this request without a question. "
                                "Your next message asks again.")
        lay.addWidget(self.all_btn)
        self.hint = QtWidgets.QLabel("<small>Nothing runs before you press Run. After a click on the code, "
                                     "Enter runs it and Esc skips it.</small>")
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)      # greyed like a hint, in the theme's own colour
        lay.addWidget(self.hint)
        self.buttons = (self.run_btn, self.skip_btn, self.all_btn)
        for button, word in zip(self.buttons, ("run", "skip", "all")):
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, word=word: self.answered.emit(word))
            button.installEventFilter(self)
        self.code.installEventFilter(self)
        self.hide()

    def ask(self, title, text):
        """Show one step. `text` is shown as it is, every character of it."""
        if hidden_characters(text):
            title += " It has characters that do not show here: look at it with care."
        self.title.setText(title)
        self.code.setPlainText(text)
        rows = min(max(text.count("\n") + 1, 1), self.ROWS)
        self.code.setFixedHeight(rows * self.code.fontMetrics().lineSpacing() + 26)
        self._since = time.monotonic()
        self.show()
        self._tell_more()                # two texts with the same scroll ranges send no signal

    def _tell_more(self, *_range):
        """Say in words when the view shows only a part of the text. A scroll bar alone is
        easy to miss, and macOS hides it until the user scrolls."""
        cut = self.code.verticalScrollBar().maximum() > 0 or self.code.horizontalScrollBar().maximum() > 0
        lines = self.code.blockCount()
        self.more.setText("<b>%d %s. Only a part is in view: scroll to read all of it.</b>"
                          % (lines, "line" if lines == 1 else "lines") if cut else "")
        self.more.setVisible(cut)

    def shown_text(self):
        return self.code.toPlainText()

    # ---- keys: Enter = Run, Esc = Skip, while the card or its text has the focus --------
    def _ours(self, event):
        return event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Escape)

    def _key(self, event, button=None):
        if not self._ours(event):
            return False
        if event.isAutoRepeat() or time.monotonic() - self._since < self.KEY_DELAY:
            return True                  # too early, or a key that is held down: dropped
        if event.key() == QtCore.Qt.Key_Escape:
            self.answered.emit("skip")
        elif button is not None:
            button.click()               # Enter on a button with the focus is that button
        else:
            self.answered.emit("run")
        return True

    def event(self, event):
        # FreeCAD has shortcuts on these keys: with the focus here they are ours
        if event.type() == QtCore.QEvent.ShortcutOverride and self._ours(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if not self._key(event):
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QtCore.QEvent.ShortcutOverride and self._ours(event):
            event.accept()
            return True
        if kind == QtCore.QEvent.KeyPress and self._key(event, obj if obj in self.buttons else None):
            return True
        return super().eventFilter(obj, event)


# ---- a new installation: who does the work? ---------------------------------------
class Chooser(QtWidgets.QFrame):
    """Three plain choices. Nothing is contacted and nothing is started before one is made."""

    chosen = QtCore.Signal(str)         # "local", "apikey" or "account"
    TITLE = "Choose the AI that does the work"
    CHOICES = (("local", "Local model", "Local model on this or another computer (nothing leaves your network)"),
               ("apikey", "API key", "Your own Anthropic API key (your messages go to Anthropic)"),
               ("account", "Claude account", "Claude account (needs the Claude Code program)"))
    FOOT = "Nothing is contacted before you choose. You can change it later in Settings."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        title = QtWidgets.QLabel(self.TITLE)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        lay.addWidget(title)
        self.buttons, self.lines = {}, {}
        for name, short, line in self.CHOICES:
            button = QtWidgets.QPushButton(short)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, name=name: self.chosen.emit(name))
            label = QtWidgets.QLabel(line)
            label.setWordWrap(True)
            lay.addWidget(button)
            lay.addWidget(label)
            self.buttons[name], self.lines[name] = button, label
        foot = QtWidgets.QLabel("<small>%s</small>" % self.FOOT)
        foot.setWordWrap(True)
        foot.setEnabled(False)
        lay.addWidget(foot)
