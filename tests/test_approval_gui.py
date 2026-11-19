# RUN: freecad-gui
"""Tests for the question before a step of the AI runs: the code, Run and Skip.

Run in the real FreeCAD GUI without a display, with a throw-away profile:

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_approval_gui.py

The profile is new: "Run the AI's code without asking" is off. The real dock,
the real built-in engine and the real bridge are used; the model is a scripted
server on this computer (local_fake_server.py) and `claude` is the pretend
program (panel_fake_claude.py). Nothing real is called.

A question waits inside FreeCAD's event handling, so the test itself stands
still until it is answered. The class User is the person at the screen: a
timer that looks at the panel and presses the buttons.
"""

import faulthandler
import http.client
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import types

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

from utf8_output import utf8_output            # noqa: E402

utf8_output()

OUT = sys.__stdout__
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_approval_gui.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []                 # exceptions inside Qt slots do not reach us any other way


def say(text):
    try:
        OUT.write(text + "\n")
        OUT.flush()
    except (OSError, ValueError, AttributeError):
        pass
    if os.environ.get("FREECAD_USER_HOME"):
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    say(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


class Client:
    """One tools/call to the bridge, sent from its own thread, as Claude Code would send it."""

    def __init__(self, url, token, name, args, ident=1):
        self.status, self.body, self.error, self.seconds = None, None, None, 0.0
        self.url, self.token = url, token
        self.message = {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                        "params": {"name": name, "arguments": args}}
        self.thread = threading.Thread(target=self._send, daemon=True)
        self.thread.start()

    def _send(self):
        t0 = time.time()
        conn = http.client.HTTPConnection("127.0.0.1", int(re.search(r":(\d+)/", self.url).group(1)), timeout=60)
        try:
            conn.request("POST", "/mcp", json.dumps(self.message).encode("utf-8"),
                         {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                          "Authorization": "Bearer " + self.token})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
            self.body = json.loads(text) if text else None
            self.status = resp.status
        except Exception as exc:
            self.error = exc
        finally:
            conn.close()
            self.seconds = time.time() - t0

    def done(self):
        return not self.thread.is_alive()

    def text(self):
        try:
            return self.body["result"]["content"][0]["text"]
        except (TypeError, KeyError, IndexError):
            return "status %s, body %s, error %r" % (self.status, self.body, self.error)


def _port(url):
    return int(re.search(r":(\d+)/", url).group(1))


def call_and_keep(url, token, name, args, ident=1):
    """Send one tools/call and give the open connection back. The test closes it without
    reading the answer: a Claude Code that gave the call up."""
    body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode("utf-8")
    sock = socket.create_connection(("127.0.0.1", _port(url)), timeout=10)
    sock.sendall(("POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n"
                  "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                  % (_port(url), token, len(body))).encode("ascii") + body)
    return sock


def cancel(url, token, ident):
    """notifications/cancelled for a request, as the MCP client of Claude Code sends it. The HTTP status."""
    conn = http.client.HTTPConnection("127.0.0.1", _port(url), timeout=10)
    try:
        conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                 "params": {"requestId": ident, "reason": "timeout"}}).encode("utf-8"),
                     {"Content-Type": "application/json", "Authorization": "Bearer " + token})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def main():
    import FreeCAD as App
    from PySide import QtCore, QtGui, QtWidgets

    # see test_panel.py: without this FreeCAD 1.1 locks up when it has no display
    QtCore.QLoggingCategory.setFilterRules("default.warning=false")

    def spin(ms=50):
        end = time.monotonic() + ms / 1000.0
        while True:
            QtWidgets.QApplication.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.005)

    def wait_for(cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            spin(20)
        return bool(cond())

    from cadprompt import approval, ask_ui, bridge, consent, local_engine, local_tools, panel, paths, projects
    from cadprompt.settings_dialog import SettingsDialog
    from local_fake_server import FakeServer, answer, call, slow
    import user_yes

    home = os.environ["FREECAD_USER_HOME"]
    p = paths.params()
    check("a new profile: 'Run the AI's code without asking' is off, and was never written",
          approval.auto() is False and "AutoApprove" not in [n for _k, n, _v in p.GetContents() or []])
    p.SetString("ProjectsDir", os.path.join(home, "projects"))
    p.SetString("Backend", "local")
    srv = FakeServer()
    p.SetString("LocalUrl", srv.url)
    p.SetString("LocalModel", "fake-model")
    project = projects.create("Approval part")
    questions = []                           # a model server on this computer: no question about a service
    consent.asker = lambda kind, url: questions.append((kind, url)) or False

    codes = []                               # every text that run_python really ran
    real_run_python = local_tools.run_python

    def recording_run_python(code, project_=""):
        codes.append(code)
        return real_run_python(code, project_)
    local_tools.run_python = recording_run_python

    panel.show()                             # the real dock in FreeCAD's window
    spin(300)
    pan = panel._dock.widget()
    card = pan.card

    def objects():
        doc = App.ActiveDocument
        return [o.Name for o in doc.Objects] if doc is not None else []

    class User:
        """The person at the screen: looks at the panel when a question opens, then acts."""

        def __init__(self):
            self.plan, self.seen, self.lost = [], [], []
            self._since = None
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._tick)
            self.timer.start(15)

        def will(self, *steps):
            """steps: (seconds after the question opened, what to do)."""
            self.plan.extend(steps)

        def _tick(self):
            if not pan.asking():
                self._since = None
                return
            now = time.monotonic()
            if self._since is None:
                self._since = now
                self.seen.append({"title": card.title.text(), "text": card.shown_text(), "ran": len(codes),
                                  "objects": objects(), "status": pan.status.text(), "hidden": card.isHidden(),
                                  "on_screen": card.isVisible(), "requests": len(srv.requests)})
            if self.plan and now - self._since >= self.plan[0][0]:
                self.plan.pop(0)[1]()
            elif not self.plan and now - self._since > 20:
                self.lost.append(card.shown_text())      # a question nobody planned for: do not hang
                card.skip_btn.click()

    user = User()

    def key(widget, which):
        return lambda: QtWidgets.QApplication.sendEvent(
            widget, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, which, QtCore.Qt.NoModifier))

    def idle():
        return not pan._running() and pan.send_btn.text() == "Send"

    def send(text, seconds=40):
        pan.input.setPlainText(text)
        pan.send()
        return wait_for(idle, seconds)

    def tool_results(first):
        """The tool results that the model got in the requests from number `first` on."""
        out = []
        for r in srv.requests[first:]:
            last = r["body"]["messages"][-1]
            if last.get("role") == "tool":
                out.append(last.get("content"))
        return out

    check("(setup) the dock is on screen with a local model on this computer: ready, nothing waits",
          panel._dock.isVisible() and isinstance(pan, panel.Panel) and card.isHidden() and not pan.asking()
          and not [e for e in pan.entries if e[0].startswith("setup")] and pan.auto_btn.isHidden(), str(pan.entries))

    # =====================================================================
    say("\n-- Run: the code is shown, the run waits, exactly that text runs --")
    code = "box = doc.addObject('Part::Box', 'Plate')\nbox.Label = 'Plate'\nbox.Length = 80   # ä, 80 mm\n"
    srv.add(call("run_python", {"code": code}, "c1"), answer("A plate."))
    n = len(srv.requests)
    user.will((0.4, card.run_btn.click))
    t0 = time.time()
    done = send("make a plate")
    seen = user.seen[-1] if user.seen else {}
    check("the question opens in the panel: the card is on screen, under the chat, no window of its own",
          done and len(user.seen) == 1 and seen["on_screen"] and not card.isWindow() and pan.isAncestorOf(card)
          and not [w for w in QtWidgets.QApplication.topLevelWidgets() if isinstance(w, ask_ui.Question)], str(seen))
    check("it shows the complete code, character for character",
          seen.get("text") == code, repr(seen.get("text")))
    check("it says who wants to run it, and the status line says that the user is waited for",
          seen.get("title") == "The local model wants to run this code in FreeCAD:"
          and seen.get("status") == "Waiting for you: Run or Skip.", str(seen))
    check("the buttons are Run, Skip and 'Run all for this request'; the text cannot be edited, the font is "
          "of fixed width, a long text scrolls",
          [b.text() for b in card.buttons] == ["Run", "Skip", "Run all for this request"]
          and card.code.isReadOnly() and card.code.maximumHeight() < 400
          and card.code.font().family() == QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont).family(), str([b.text() for b in card.buttons]))
    check("while the question is open nothing has run: no code, no document, no further request",
          seen.get("ran") == 0 and seen.get("objects") == [] and seen.get("requests") == n + 1, str(seen))
    check("the run waited for the click (0.4 s)", time.time() - t0 >= 0.4, f"{time.time() - t0:.2f} s")
    check("Run runs exactly the text that was shown, one time", codes == [code] and "Plate" in objects(), str(codes))
    check("the model gets the result of the step", any("+ new Plate" in (t or "") for t in tool_results(n)),
          str(tool_results(n)))
    check("afterwards the card is gone, the panel is idle, and the chat keeps the code that ran",
          card.isHidden() and not pan.asking() and pan.status.text() == "" and ("code", code) in pan.entries
          and ("claude", "A plate.") in pan.entries, str(pan.entries[-5:]))
    plain = pan.view.toPlainText()
    check("in the chat the code is folded: one line 'ran: 4 lines of code · show'",
          "ran: 4 lines of code" in plain and "box.Length = 80" not in plain
          and 'href="cadprompt:code/' in pan.view.toHtml(), plain[-300:])
    i = pan.entries.index(("code", code))
    pan._on_link(QtCore.QUrl("cadprompt:code/%d" % i))
    check("'show' opens it in the chat, 'hide' folds it again",
          "box.Length = 80   # ä, 80 mm" in pan.view.toPlainText(), pan.view.toPlainText()[-300:])
    pan._on_link(QtCore.QUrl("cadprompt:code/%d" % i))
    check("... folded again", "box.Length = 80" not in pan.view.toPlainText())

    # =====================================================================
    say("\n-- Skip: nothing runs, the AI is told --")
    code2 = "doc.addObject('Part::Box', 'NotWanted')"
    srv.add(call("run_python", {"code": code2}, "c2"), answer("Then I leave it."))
    n, n_codes = len(srv.requests), len(codes)
    user.will((0.2, card.skip_btn.click))
    done = send("one more box")
    check("Skip: nothing ran, no object", done and len(codes) == n_codes and "NotWanted" not in objects()
          and user.seen[-1]["text"] == code2, str(codes[n_codes:]))
    check("Skip: the AI gets 'The user did not approve this step.' and goes on",
          tool_results(n) == [approval.DECLINED] and approval.DECLINED == "The user did not approve this step."
          and ("claude", "Then I leave it.") in pan.entries, str(tool_results(n)))
    check("Skip: the chat says that the code was skipped, not run",
          ("skipped", code2) in pan.entries and "skipped, not run" in pan.view.toPlainText()
          and ("code", code2) not in pan.entries, str(pan.entries[-4:]))

    # =====================================================================
    say("\n-- tools that only look are not asked for --")
    srv.add(call("get_document_state", {}, "c3"), call("read_project_file", {"path": "refs/INDEX.md"}, "c4"),
            answer("Looked."))
    n, n_seen = len(srv.requests), len(user.seen)
    done = send("look at it")
    got = tool_results(n)
    check("get_document_state and read_project_file run without a question",
          done and len(user.seen) == n_seen and len(got) == 2 and "Plate" in got[0] and got[1].startswith("FILE refs/INDEX.md"),
          str(got)[:300])

    # =====================================================================
    say("\n-- make_drawing asks with one line --")
    line = "Make drawing: sheet A4, object Plate → drawings/Plate-A4.pdf"
    pdf = os.path.join(project, "drawings", "Plate-A4.pdf")
    srv.add(call("make_drawing", {"sheet": "A4"}, "c5"), answer("No drawing then."))
    n = len(srv.requests)
    user.will((0.2, card.skip_btn.click))
    done = send("make the drawing")
    seen = user.seen[-1]
    check("make_drawing: the question is one line that names the sheet, the object and the PDF",
          done and seen["text"] == line and seen["title"] == "The local model wants to make a drawing:", str(seen))
    check("make_drawing, Skip: no page, no PDF, the AI is told",
          tool_results(n) == [approval.DECLINED] and not os.path.exists(pdf)
          and not [o for o in App.ActiveDocument.Objects if o.TypeId == "TechDraw::DrawPage"], str(tool_results(n)))
    srv.add(call("make_drawing", {"sheet": "A4"}, "c6"), answer("The drawing is made."))
    n = len(srv.requests)
    user.will((0.2, card.run_btn.click))
    done = send("make the drawing now", 120)
    check("make_drawing, Run: the drawing is made, and its PDF is the one that the line named",
          done and os.path.isfile(pdf) and (tool_results(n) or [""])[0].startswith("Drawing made")
          and ("code", line) in pan.entries, str(tool_results(n))[:300])
    check("... in the chat a one-line step is shown as it is, with no 'show'", line in pan.view.toPlainText())

    # =====================================================================
    say("\n-- keys: Enter runs, Esc skips --")
    card.KEY_DELAY = 0.4
    early = []
    srv.add(call("run_python", {"code": "k1 = 1"}, "c7"), answer("ok"))
    n_codes = len(codes)
    user.will((0.05, key(card.code, QtCore.Qt.Key_Return)),
              (0.25, lambda: early.append((pan.asking(), len(codes) - n_codes))),
              (0.7, key(card.code, QtCore.Qt.Key_Return)))
    done = send("key test 1")
    check("Enter in the first moment does nothing: a key on its way to another text must not run code",
          early == [(True, 0)], str(early))
    check("Enter with the focus in the card is Run", done and codes[n_codes:] == ["k1 = 1"], str(codes[n_codes:]))
    srv.add(call("run_python", {"code": "k2 = 2"}, "c8"), answer("ok"))
    n, n_codes = len(srv.requests), len(codes)
    user.will((0.6, key(card, QtCore.Qt.Key_Escape)))
    done = send("key test 2")
    check("Esc is Skip", done and len(codes) == n_codes and tool_results(n) == [approval.DECLINED], str(tool_results(n)))
    srv.add(call("run_python", {"code": "k3 = 3"}, "c9"), answer("ok"))
    n, n_codes = len(srv.requests), len(codes)
    user.will((0.6, key(card.skip_btn, QtCore.Qt.Key_Enter)))
    done = send("key test 3")
    check("Enter on the Skip button, when that has the focus, is Skip and not Run",
          done and len(codes) == n_codes and tool_results(n) == [approval.DECLINED], str(tool_results(n)))
    card.KEY_DELAY = 0.0
    srv.add(call("run_python", {"code": "k4 = 4"}, "c10"), answer("ok"))
    n_codes = len(codes)
    user.will((0.1, key(card.code, QtCore.Qt.Key_A)), (0.3, lambda: early.append(pan.asking())),
              (0.4, card.run_btn.click))
    done = send("key test 4")
    check("another key does nothing", done and early[-1] is True and codes[n_codes:] == ["k4 = 4"])
    # A key that is held down repeats. The second step of a request opens a moment after the
    # Run of the first one: the Enter that is still down must not answer it.
    card.KEY_DELAY = 0.0
    srv.add(call("run_python", {"code": "k5 = 5"}, "c11"), answer("ok"))
    n_codes = len(codes)
    held = lambda: QtWidgets.QApplication.sendEvent(card.code, QtGui.QKeyEvent(
        QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return, QtCore.Qt.NoModifier, "\r", True))
    user.will((0.2, held), (0.25, held), (0.3, lambda: early.append((pan.asking(), len(codes) - n_codes))),
              (0.4, key(card.code, QtCore.Qt.Key_Return)))
    done = send("key test 5")
    check("an Enter that is held down (key repeat) does not answer; a new press does",
          done and early[-1] == (True, 0) and codes[n_codes:] == ["k5 = 5"], str(early[-1:]) + str(codes[n_codes:]))

    # The card does not take the keyboard. After Send the focus is in the empty message box,
    # and the user can type the next message with the eyes on the keyboard: those keys go into
    # the box (not to FreeCAD's shortcuts), and the Enter at their end answers nothing.
    import FreeCADGui as Gui
    app, window = QtWidgets.QApplication.instance(), Gui.getMainWindow()
    for _ in range(20):
        window.activateWindow()
        pan.input.setFocus()
        spin(100)
        if app.focusWidget() is pan.input:
            break
    in_box = app.focusWidget() is pan.input
    if not in_box:
        say("   (this platform gave the message box no focus: only 'the card takes none' is checked)")

    def press(which, text):
        widget = app.focusWidget() or pan.input
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            app.sendEvent(widget, QtGui.QKeyEvent(kind, which, QtCore.Qt.NoModifier, text))

    def in_card():
        return card.hasFocus() or any(w.hasFocus() for w in card.findChildren(QtWidgets.QWidget))
    typed = {}

    def type_on():
        typed["focus"] = (in_card(), app.focusWidget() is pan.input)
        press(QtCore.Qt.Key_M, "m")
        press(QtCore.Qt.Key_Return, "\r")
        spin(50)
        typed["after"] = (pan.asking(), len(codes) - n_codes, pan.input.toPlainText())
    card.KEY_DELAY = 0.0                     # the delay is not what protects here
    srv.add(call("run_python", {"code": "k6 = 6"}, "c12"), answer("ok"))
    n, n_codes = len(srv.requests), len(codes)
    user.will((0.5, type_on), (0.7, card.skip_btn.click))
    pan.input.setPlainText("key test 6")
    press(QtCore.Qt.Key_Return, "\r") if in_box else pan.send()     # Send with Enter: the box is empty, the focus in it
    done = wait_for(idle, 40)
    check("the card does not take the keyboard from the message box: a letter and Enter typed after it opened "
          "run nothing and answer nothing" + (", and the letter is in the message box" if in_box else ""),
          done and typed.get("focus") == (False, in_box) and typed.get("after", ())[:2] == (True, 0)
          and (not in_box or typed["after"][2] == "m") and len(codes) == n_codes
          and tool_results(n) == [approval.DECLINED], str(typed))
    pan.input.clear()
    card.KEY_DELAY = 0.4

    # A text that the view cuts: the card says so in words. A scroll bar alone is easy to
    # miss (macOS hides it), and the part that is out of view runs too.
    notes = []
    note = lambda: notes.append(card.more.text() if card.more.isVisible() else None)
    long_code = "\n".join("v%d = %d" % (i, i) for i in range(52))
    wide_code = "w = 1" + " " * 300 + "; w2 = 2"
    for text in (long_code, wide_code, "s = 1\ns = 2\ns = 3"):
        srv.add(call("run_python", {"code": text}, "m%d" % len(notes)), answer("ok"))
        user.will((0.3, note), (0.35, card.run_btn.click))
        send("long text %d" % len(notes))
    check("code that is longer than the view: the card says how many lines it has and that only a part is in view",
          len(notes) == 3 and notes[0] is not None and "52 lines." in notes[0] and "scroll" in notes[0]
          and codes[-3] == long_code, repr(notes))
    check("one line that is wider than the view (300 spaces, then more code): the card says so too",
          len(notes) == 3 and notes[1] is not None and "1 line." in notes[1] and "scroll" in notes[1]
          and codes[-2] == wide_code, repr(notes))
    check("code that is all in view has no such note", len(notes) == 3 and notes[2] is None, repr(notes))

    check("a text with characters that do not show is marked",
          ask_ui.hidden_characters("x = 1 ‮# hidden") and ask_ui.hidden_characters("a​b")
          and not ask_ui.hidden_characters(code) and not ask_ui.hidden_characters("a\tb\n"))

    # =====================================================================
    say("\n-- 'Run all for this request' ends with the request --")
    srv.add(call("run_python", {"code": "a1 = 1"}, "d1"), call("run_python", {"code": "a2 = a1 + 1"}, "d2"),
            call("make_drawing", {"sheet": "A4"}, "d3"), answer("Three steps."))
    n_seen, n_codes = len(user.seen), len(codes)
    user.will((0.2, card.all_btn.click))
    done = send("three steps", 120)
    check("'Run all for this request': one question, then the other steps of this request run without one",
          done and len(user.seen) == n_seen + 1 and codes[n_codes:] == ["a1 = 1", "a2 = a1 + 1"]
          and pan.entries.count(("code", line)) == 2, f"{len(user.seen) - n_seen} questions, {codes[n_codes:]}")
    check("... the code of every step is in the chat all the same",
          ("code", "a1 = 1") in pan.entries and ("code", "a2 = a1 + 1") in pan.entries)
    check("... and it is not the switch of Settings", approval.auto() is False and pan.auto_btn.isHidden())
    srv.add(call("run_python", {"code": "a3 = 3"}, "d4"), answer("ok"))
    n_seen = len(user.seen)
    user.will((0.2, card.run_btn.click))
    done = send("the next request")
    check("the next request asks again", done and len(user.seen) == n_seen + 1 and user.seen[-1]["text"] == "a3 = 3")

    # =====================================================================
    say("\n-- Stop, a new chat and a closed panel while a question waits --")

    def ended(what, act, entry):
        srv.add(call("run_python", {"code": "doc.addObject('Part::Box', 'Never')"}, "s1"))
        n, n_codes, t0 = len(srv.requests), len(codes), time.time()
        user.will((0.3, act))
        pan.input.setPlainText("a step that is never approved")
        pan.send()
        done = wait_for(idle, 20)
        took = time.time() - t0
        wait_for(lambda: all(w.isFinished() for w in local_engine._threads), 5)
        spin(300)
        check(f"{what} while a question waits: the request ends at once, no hang",
              done and took < 10 and not pan.asking() and card.isHidden(), f"{took:.1f} s")
        check(f"{what}: nothing ran, and no further request went to the model",
              len(codes) == n_codes and "Never" not in objects() and len(srv.requests) == n + 1
              and all(w.isFinished() for w in local_engine._threads), f"{codes[n_codes:]} {len(srv.requests) - n}")
        if entry:
            check(f"{what}: the chat ends with {entry!r}", pan.entries[-1] == entry, str(pan.entries[-2:]))
        srv.script[:] = []

    ended("Stop", pan.send_btn.click, ("note", "Stopped."))
    newchat = [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0]
    ended("New chat", newchat.click, ("note", "New conversation."))
    ended("the dock is closed with its command", panel.toggle, ("note", "Stopped."))
    check("... the dock is hidden, and comes back with the command", not panel._dock.isVisible())
    panel.toggle()
    spin(100)
    check("... back on screen, ready for the next message", panel._dock.isVisible() and idle())
    # FreeCAD is closed: its main window goes away while the question waits. A stand-in window
    # is closed here, so that the real one stays.
    stand_in = QtWidgets.QWidget()
    stand_in.show()
    real_gui, ask_ui.Gui = ask_ui.Gui, types.SimpleNamespace(getMainWindow=lambda: stand_in)
    try:
        ended("FreeCAD's window is closed", stand_in.hide, None)
    finally:
        ask_ui.Gui = real_gui
        stand_in.deleteLater()

    # the panel is closed while the AI still thinks: its question must not wait where nobody sees it
    srv.add(slow(3.0, call("run_python", {"code": "late = 1"}, "h1")), answer("ok"))
    n_seen, n_codes = len(user.seen), len(codes)
    pan.input.setPlainText("think first")
    pan.send()
    spin(200)
    panel.toggle()                           # no question waits yet: the request goes on
    hidden = not panel._dock.isVisible() and pan._running()
    user.will((0.3, card.run_btn.click))
    done = wait_for(idle, 30)
    check("the dock is closed while the AI works, then a question comes: the dock comes back with the question, "
          "and Run runs the step",
          hidden and done and len(user.seen) == n_seen + 1 and user.seen[-1]["on_screen"] and panel._dock.isVisible()
          and codes[n_codes:] == ["late = 1"], f"{hidden} {user.seen[-1]} {codes[n_codes:]}")

    opened = []
    real_open = QtGui.QDesktopServices.openUrl
    QtGui.QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toString()) or True)
    try:
        for link in ("https://example.org/a", "http://example.org/b", "file:///etc/passwd", "smb://host/share",
                     "cadprompt:code/99999", "javascript:alert(1)"):
            pan._on_link(QtCore.QUrl(link))
    finally:
        QtGui.QDesktopServices.openUrl = real_open
    check("a link in the chat: a web link opens in the browser, a link to a file or a program does nothing",
          opened == ["https://example.org/a", "http://example.org/b"], str(opened))

    # =====================================================================
    say("\n-- the switch 'Run the AI's code without asking' --")
    warnings, verdict = [], [False]
    real_confirm = ask_ui.confirm

    def recording_confirm(parent, headline, text, yes, no):
        warnings.append((headline, text, yes, no))
        return verdict[0]
    ask_ui.confirm = recording_confirm
    dlg = SettingsDialog(pan)
    check("Settings has the checkbox 'Run the AI's code without asking', and it is off",
          dlg.auto.text() == "Run the AI's code without asking" and not dlg.auto.isChecked())
    dlg.auto.click()
    check("turning it on shows a warning first; 'Keep asking' leaves it off",
          len(warnings) == 1 and not dlg.auto.isChecked() and approval.auto() is False, str(warnings)[:200])
    check("the warning says it in plain words: the code runs as you, it can change or delete files, use it for "
          "models you can restore, each run is one Undo step, a backup copy is made first",
          warnings[0][0] == "Run the AI's code without asking?" and warnings[0][2:] == ("Run without asking", "Keep asking")
          and all(w in warnings[0][1] for w in ("runs as you", "change or delete files", "models that you can restore",
                                                "Each run is one Undo step", "backup copy of the model file")),
          str(warnings[0]))
    verdict[0] = True
    dlg.auto.click()
    check("'Run without asking' in the warning turns the checkbox on, and nothing is saved before OK",
          len(warnings) == 2 and dlg.auto.isChecked() and approval.auto() is False)
    dlg.auto.click()
    check("turning it off needs no warning", len(warnings) == 2 and not dlg.auto.isChecked())
    dlg.auto.click()
    dlg.reject()
    dlg.deleteLater()
    check("Cancel in Settings keeps asking", approval.auto() is False and pan.auto_btn.isHidden())

    # the real warning window, one time: a click on "Keep asking"
    ask_ui.confirm = real_confirm
    looked = []

    def answer_window(button):
        def act():
            open_ = [w for w in QtWidgets.QApplication.topLevelWidgets()
                     if isinstance(w, ask_ui.Question) and not w.isHidden()]
            if not open_:
                looked.append(None)
                return
            w = open_[0]
            looked.append({"headline": w.headline.text(), "text": w.text.text(), "modal": w.isModal(),
                           "buttons": (w.yes_btn.text(), w.no_btn.text()),
                           "default": [b.text() for b in (w.yes_btn, w.no_btn) if b.isDefault()]})
            getattr(w, button).click()
        return act
    dlg = SettingsDialog(pan)
    QtCore.QTimer.singleShot(400, answer_window("no_btn"))
    dlg.auto.click()
    check("the real warning is a small window over Settings; Enter is 'Keep asking'",
          bool(looked) and looked[0] is not None and looked[0]["headline"] == approval.WARNING_TITLE
          and looked[0]["text"] == approval.WARNING and looked[0]["buttons"] == ("Run without asking", "Keep asking")
          and looked[0]["default"] == ["Keep asking"] and looked[0]["modal"], str(looked)[:300])
    check("... 'Keep asking' in that window leaves the checkbox off", not dlg.auto.isChecked())
    QtCore.QTimer.singleShot(400, answer_window("yes_btn"))
    dlg.auto.click()
    check("... 'Run without asking' in that window turns it on", dlg.auto.isChecked() and len(looked) == 2)
    dlg.reject()
    dlg.deleteLater()
    ask_ui.confirm = recording_confirm

    def settings(change):
        """Open the real dialog as the panel does, let `change` click in it, press OK."""
        def fake_exec(d):
            change(d)
            d._save()
            return d.result()
        SettingsDialog.exec = fake_exec
        try:
            pan._settings()
        finally:
            del SettingsDialog.exec
        spin(100)

    settings(lambda d: d.auto.click())
    check("OK in Settings saves it, and the panel shows the marker 'runs without asking'",
          approval.auto() is True and p.GetBool("AutoApprove", False) is True and not pan.auto_btn.isHidden()
          and pan.auto_btn.isVisible() and pan.auto_btn.text() == "runs without asking", pan.auto_btn.text())
    code3 = "s1 = 1\ns2 = 2\ns3 = s1 + s2"
    srv.add(call("run_python", {"code": code3}, "e1"), answer("ok"))
    n_seen, n_codes = len(user.seen), len(codes)
    done = send("with the switch on")
    check("with the switch on the code runs without a question",
          done and len(user.seen) == n_seen and codes[n_codes:] == [code3], str(codes[n_codes:]))
    plain = pan.view.toPlainText()
    check("... and it is still in the chat, folded, so the user can read what ran",
          ("code", code3) in pan.entries and "ran: 3 lines of code" in plain and "s3 = s1 + s2" not in plain)
    pan._on_link(QtCore.QUrl("cadprompt:code/%d" % pan.entries.index(("code", code3))))
    check("... 'show' opens it", "s3 = s1 + s2" in pan.view.toPlainText())
    check("the marker stays while the switch is on", not pan.auto_btn.isHidden())
    pan.auto_btn.click()
    check("one click on the marker turns the switch off: saved, the marker is gone, the chat says so",
          approval.auto() is False and p.GetBool("AutoApprove", True) is False and pan.auto_btn.isHidden()
          and pan.entries[-1] == ("note", "From now on CADprompt asks before it runs the AI's code."),
          str(pan.entries[-1]))
    srv.add(call("run_python", {"code": "s4 = 4"}, "e2"), answer("ok"))
    n_seen, n_codes = len(user.seen), len(codes)
    user.will((0.2, card.skip_btn.click))
    done = send("after the click")
    check("... and the next step asks again", done and len(user.seen) == n_seen + 1 and len(codes) == n_codes)
    dlg = SettingsDialog(pan)
    check("... Settings shows the checkbox off again", not dlg.auto.isChecked())
    dlg.reject()
    dlg.deleteLater()

    # =====================================================================
    say("\n-- Claude Code: the same question, for a tool call that comes through the bridge --")
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir)
    python = os.environ.get("CADPROMPT_TEST_PYTHON") or shutil.which("python3") or shutil.which("python") or "python3"
    script = os.path.join(HERE, "panel_fake_claude.py")
    if os.name == "nt":
        fake_claude = os.path.join(bindir, "claude.cmd")
        with open(fake_claude, "w") as f:
            f.write('@echo off\n"%s" "%s" %%*\n' % (python, script))
    else:
        fake_claude = os.path.join(bindir, "claude")
        with open(fake_claude, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (python, script))
        os.chmod(fake_claude, 0o755)
    claude_log = os.path.join(home, "fake-claude.jsonl")
    os.environ["PANEL_FAKE_CLAUDE_LOG"] = claude_log
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(home, "claude-config")

    def claude_calls():
        try:
            with open(claude_log, encoding="utf-8") as f:
                return [json.loads(row) for row in f]
        except OSError:
            return []

    p.SetString("claudePath", fake_claude)
    user_yes.allow_claude_code()             # the question about the service is in test_consent_gui.py
    p.SetString("Backend", "account")
    pan._show_source()
    pan.check_setup()
    spin(600)                                # `claude auth status` answers
    check("(setup) the panel is ready for the Claude account, the bridge does not listen yet",
          pan.claude == fake_claude and not bridge.running()
          and not [e for e in pan.entries if e[0] == "setup"], str(pan.entries[-2:]))

    def run_fake(text, seconds=40):
        n = len(claude_calls())
        pan.send(text)
        wait_for(lambda: idle() and len([c for c in claude_calls()[n:] if c["argv"][:1] == ["-p"]]) > 0, seconds)
        runs = [c for c in claude_calls()[n:] if c["argv"][:1] == ["-p"]]
        return runs[-1] if runs else {}

    for d in list(App.listDocuments()):
        App.closeDocument(d)
    code4 = "box = doc.addObject('Part::Box', 'FromClaude')\nbox.Length = 20"
    n_seen, n_codes = len(user.seen), len(codes)
    user.will((1.0, card.run_btn.click))
    rec = run_fake("mcp: " + code4)
    seen = user.seen[-1] if len(user.seen) > n_seen else {}
    status, body = (rec.get("mcp") or {}).get("call") or (0, {})
    check("the pretend Claude Code calls run_python through the real bridge: the question opens, with the "
          "exact code, and names Claude",
          seen.get("text") == code4 and seen.get("title") == "Claude wants to run this code in FreeCAD:"
          and seen.get("on_screen"), str(seen))
    check("... while it is open nothing has run", seen.get("ran") == n_codes and seen.get("objects") == [], str(seen))
    check("... the request of Claude Code waited for the click, and Run ran exactly that code",
          status == 200 and codes[n_codes:] == [code4] and "FromClaude" in objects()
          and "+ new FromClaude" in body["result"]["content"][0]["text"] and body["result"]["isError"] is False,
          str((rec.get("mcp") or {}).get("call"))[:400])
    check("... and the chat keeps the code", ("code", code4) in pan.entries and card.isHidden())
    n_seen, n_codes = len(user.seen), len(codes)
    user.will((0.3, card.skip_btn.click))
    rec = run_fake("mcp: doc.addObject('Part::Box', 'NotFromClaude')")
    status, body = (rec.get("mcp") or {}).get("call") or (0, {})
    check("Skip for Claude Code: nothing runs, and its tool call gets 'The user did not approve this step.'",
          len(user.seen) == n_seen + 1 and len(codes) == n_codes and "NotFromClaude" not in objects()
          and status == 200 and body["result"]["content"][0]["text"] == approval.DECLINED
          and body["result"]["isError"] is False, str((rec.get("mcp") or {}).get("call"))[:400])

    # a client thread of our own, while the pretend Claude Code is at work for a minute
    n = len(claude_calls())
    pan.send("slow: think for a minute")
    wait_for(lambda: bridge.running() and [c for c in claude_calls()[n:] if c["stdin"].startswith("slow")], 20)
    url, token = bridge._server.url, bridge.token()
    check("(setup) Claude Code works, the bridge listens", pan.send_btn.text() == "Stop" and bridge.running())
    n_seen = len(user.seen)
    c = Client(url, token, "get_document_state", {})
    wait_for(c.done, 20)
    check("a tool call that only looks comes back without a question",
          c.status == 200 and "FromClaude" in c.text() and len(user.seen) == n_seen, c.text()[:200])
    n_codes = len(codes)
    user.will((1.5, card.run_btn.click))
    c = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'FromThread')"})
    wait_for(c.done, 30)
    check("a run_python call from a client thread: the request waits in its thread while the user decides "
          "(1.5 s), then runs",
          c.status == 200 and c.seconds >= 1.5 and "+ new FromThread" in c.text()
          and user.seen[-1]["text"] == "doc.addObject('Part::Box', 'FromThread')"
          and user.seen[-1]["objects"].count("FromThread") == 0 and codes[n_codes:] == [user.seen[-1]["text"]],
          f"{c.seconds:.1f} s {c.text()[:200]}")
    n_codes = len(codes)
    user.will((0.3, card.skip_btn.click))
    c = Client(url, token, "make_drawing", {"sheet": "A3", "obj_name": "FromClaude"})
    wait_for(c.done, 30)
    check("make_drawing from Claude Code asks with its one line, and Skip makes nothing",
          c.text() == approval.DECLINED
          and user.seen[-1]["text"] == "Make drawing: sheet A3, object FromClaude → drawings/FromClaude-A3.pdf"
          and user.seen[-1]["title"] == "Claude wants to make a drawing:", str(user.seen[-1]))

    # Claude Code gives a tool call up (its own time limit, a lost connection) while the card
    # waits. Nobody takes the result any more, and the model can send the same call again.
    # (A question that is open keeps this script inside its wait: the user's timer does the work.)
    not_waited = ("note", panel.NOT_WAITED % "Claude")
    late_code = "doc.addObject('Part::Box', 'LateBox')"
    n_codes, n_seen, n_notes = len(codes), len(user.seen), pan.entries.count(not_waited)
    sock = call_and_keep(url, token, "run_python", {"code": late_code})
    user.will((0.5, sock.close))
    closed = wait_for(lambda: len(user.seen) > n_seen and not pan.asking() and card.isHidden(), 20)
    check("the client of a tool call goes away while its question waits: the question closes by itself, "
          "nothing runs, the chat says that the step was not run, and Claude Code still works",
          closed and len(user.seen) == n_seen + 1 and user.seen[-1]["text"] == late_code and len(codes) == n_codes
          and "LateBox" not in objects() and pan.entries.count(not_waited) == n_notes + 1
          and pan.send_btn.text() == "Stop" and pan.status.text() == "Claude is working… (run_python)",
          f"{closed} {pan.entries[-2:]} {pan.status.text()!r}")
    user.will((0.3, card.run_btn.click))
    c = Client(url, token, "run_python", {"code": late_code})
    wait_for(c.done, 30)
    spin(300)
    check("... the same call sent again asks one time and runs one time",
          c.status == 200 and len(user.seen) == n_seen + 2 and codes[n_codes:] == [late_code]
          and [o for o in objects() if o.startswith("LateBox")] == ["LateBox"] and not pan.asking(),
          f"{c.text()[:200]} {codes[n_codes:]} {objects()}")
    n_codes, n_seen = len(codes), len(user.seen)
    behind = []
    user.will((0.3, lambda: behind.append(call_and_keep(
                  url, token, "run_python", {"code": "doc.addObject('Part::Box', 'NobodyWaits')"}, 2))),
              (0.7, lambda: behind[0].close()),        # this one waits behind the card of the first call
              (1.2, card.run_btn.click))
    first = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'FirstOfTwo')"})
    wait_for(first.done, 30)
    spin(600)
    check("a call that waits behind the card of another one, and whose client goes away: no card for it "
          "afterwards, and its code does not run",
          first.status == 200 and codes[n_codes:] == ["doc.addObject('Part::Box', 'FirstOfTwo')"]
          and len(user.seen) == n_seen + 1 and "NobodyWaits" not in objects() and not pan.asking(),
          f"{codes[n_codes:]} {len(user.seen) - n_seen} {objects()}")
    n_codes, n_seen, n_notes = len(codes), len(user.seen), pan.entries.count(not_waited)
    got = {}
    user.will((0.3, lambda: got.update(other=cancel(url, token, 99))),
              (0.6, lambda: got.update(still=pan.asking())),
              (0.7, lambda: got.update(status=cancel(url, token, 7))))
    c = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'Cancelled')"}, ident=7)
    wait_for(lambda: c.done() and not pan.asking(), 30)
    spin(200)
    check("Claude Code cancels the call and keeps its connection open (notifications/cancelled): the same. "
          "A cancel for another request changes nothing",
          got == {"other": 202, "still": True, "status": 202} and c.text() == bridge.GONE and card.isHidden()
          and len(user.seen) == n_seen + 1 and len(codes) == n_codes and "Cancelled" not in objects()
          and pan.entries.count(not_waited) == n_notes + 1 and pan.send_btn.text() == "Stop",
          f"{got} {c.text()[:200]}")
    user.will((0.3, card.run_btn.click))
    c = Client(url, token, "run_python", {"code": "after_cancel = 7"}, ident=7)
    wait_for(c.done, 30)
    check("... a new call with the same id is asked for and runs: the old cancel does not count for it",
          c.status == 200 and codes[n_codes:] == ["after_cancel = 7"] and c.text() != bridge.GONE, c.text()[:200])

    n_codes = len(codes)
    user.will((0.3, pan.send_btn.click))     # Stop while the question for Claude Code waits
    c = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'AfterStop')"})
    wait_for(lambda: c.done() and idle(), 30)
    spin(300)
    check("Stop while a question for Claude Code waits: nothing runs, its call is answered, the panel is idle",
          c.done() and c.status == 200 and "AfterStop" not in objects() and c.text() in (approval.DECLINED, bridge.GONE)
          and len(codes) == n_codes and idle() and card.isHidden() and pan.entries[-1] == ("note", "Stopped."),
          f"{c.text()[:200]} {pan.entries[-1]}")
    late = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'Late')"})
    wait_for(late.done, 20)
    check("... and a call that comes in after Stop, with the right token, runs nothing and asks nobody",
          "Late" not in objects() and not pan.asking() and (late.error is not None or late.text() == bridge.GONE),
          late.text()[:200])


    # New chat while a question of Claude Code waits: the old request ends, as for a local model
    newchat = [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0]
    projects.set_session(pan.project, "session-of-the-old-chat")
    n = len(claude_calls())
    pan.send("slow: think for a minute more")
    wait_for(lambda: bridge.running() and [c for c in claude_calls()[n:] if c["stdin"].startswith("slow")], 20)
    url, token = bridge._server.url, bridge.token()
    at_work = pan.send_btn.text() == "Stop" and pan.run.running()
    n_codes = len(codes)
    user.will((0.3, newchat.click))
    c = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'OldChat')"})
    wait_for(lambda: c.done() and idle(), 30)
    spin(300)
    check("New chat while a question for Claude Code waits: Claude Code is stopped, nothing runs, the status "
          "line is empty and the chat is the new one",
          at_work and c.done() and idle() and not pan.run.running() and card.isHidden() and not pan.asking()
          and pan.status.text() == "" and pan.entries == [("note", "New conversation.")]
          and len(codes) == n_codes and "OldChat" not in objects()
          and projects.get_session(pan.project) is None,
          f"{at_work} {c.text()[:120]} {pan.status.text()!r} {pan.entries} {projects.get_session(pan.project)}")
    again = Client(url, token, "run_python", {"code": "doc.addObject('Part::Box', 'OldChatAgain')"})
    wait_for(again.done, 20)
    spin(300)
    check("... a later step of the old conversation opens no card in the new chat, and the old conversation "
          "is not written back",
          not pan.asking() and "OldChatAgain" not in objects() and pan.entries == [("note", "New conversation.")]
          and (again.error is not None or again.text() == bridge.GONE)
          and projects.get_session(pan.project) is None, again.text()[:200])

    check("the user was asked no question about a service in all of this", not questions, str(questions))
    check("every question had its answer planned", not user.lost and not user.plan, f"{user.lost} {len(user.plan)}")
    user.timer.stop()
    bridge.stop()
    srv.close()
    check("no exception in any Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))


def _hook(kind, value, tb):
    SLOT_ERRORS.append("".join(traceback.format_exception(kind, value, tb)))


if not os.environ.get("FREECAD_USER_HOME"):
    say("Set FREECAD_USER_HOME to an empty folder first: this test must not touch your FreeCAD settings.")
    os._exit(2)
sys.excepthook = _hook
faulthandler.dump_traceback_later(270, exit=True, file=OUT)     # a hang ends with a trace, not silence
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
