# RUN: freecad-gui
"""Tests for the question before data leaves the computer, and for a new installation.

Run in the real FreeCAD GUI without a display, with a throw-away profile:

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_consent_gui.py

The profile is new: no AI is chosen and no service has the user's yes. The
real dock, the real settings dialog, the real built-in engine and the real
bridge are used. The services are scripted servers on this computer
(local_fake_server.py) that count every connection, and `claude` is the
pretend program (panel_fake_claude.py), which writes a line for every start.

A server "on another computer" is a made-up name that ends in .test: this
file answers the name look-up itself with 127.0.0.1, so the connection, if
one is made, arrives at a counting server. CADprompt only sees the name, and
for it the name is another computer.

The question is a window that waits inside FreeCAD's event handling. The
class Person is the user at the screen: a timer that looks for the window,
writes down what it says and what was contacted until then, and presses
Allow or Not now.
"""

import datetime
import faulthandler
import json
import os
import shutil
import socket
import sys
import time
import traceback

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

from utf8_output import utf8_output            # noqa: E402

utf8_output()

OUT = sys.__stdout__
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_consent_gui.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []                 # exceptions inside Qt slots do not reach us any other way
ENV_KEY = "sk-ant-FROM-THE-ENVIRONMENT-made-up"
SAVED_KEY = "sk-ant-saved-in-settings-made-up"


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


def main():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets

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

    home = os.environ["FREECAD_USER_HOME"]

    # ---- a key and a token in the environment of the system: nothing of ours may use them ----
    os.environ["ANTHROPIC_API_KEY"] = ENV_KEY
    os.environ["ANTHROPIC_AUTH_TOKEN"] = ENV_KEY
    os.environ["OPENAI_API_KEY"] = ENV_KEY

    # ---- the pretend claude, first in the PATH: a start of `claude` is a line in its log ----
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
    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    claude_log = os.path.join(home, "fake-claude.jsonl")
    os.environ["PANEL_FAKE_CLAUDE_LOG"] = claude_log
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(home, "claude-config")

    def claude_calls():
        try:
            with open(claude_log, encoding="utf-8") as f:
                return [json.loads(row) for row in f]
        except OSError:
            return []

    # ---- names that end in .test are "other computers" that this file answers for ----
    lookups = []
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kw):
        lookups.append(host)
        if isinstance(host, str) and host.endswith(".test"):
            host = "127.0.0.1"
        return real_getaddrinfo(host, *args, **kw)
    socket.getaddrinfo = getaddrinfo

    from cadprompt import (approval, ask_ui, backends, bridge, consent, local_agent, panel, paths, projects,
                           runner, workbench)
    from cadprompt.settings_dialog import NOT_ALLOWED, PRIVACY_NONE, SettingsDialog
    from local_fake_server import FakeAnthropic, FakeServer, answer, claude

    here = FakeServer()                      # a model server on this computer
    room = FakeServer()                      # a model server on another computer in the room
    room2 = FakeServer()                     # and one more
    anth = FakeAnthropic()                   # plays Anthropic
    port = lambda srv: srv.httpd.server_address[1]
    ROOM = "http://pc-in-the-room.test:%d/v1" % port(room)
    ROOM2 = "http://another-pc.test:%d/v1" % port(room2)
    backends.ANTHROPIC_URL = "http://api.anthropic.test:%d" % port(anth)
    servers = (here, room, room2, anth)

    def contacted():
        """Every connection that any of the servers saw, also one that sent nothing."""
        return sum(s.connections for s in servers)

    connects, starts = [], []                # the two doors themselves: every call is written down
    real_connect, real_start = local_agent.connect, runner._start

    def counting_connect(address, *args, **kw):
        connects.append(address)
        return real_connect(address, *args, **kw)

    def counting_start(proc, program, args):
        starts.append([program] + list(args))
        return real_start(proc, program, args)
    local_agent.connect, runner._start = counting_connect, counting_start

    def nothing_left():
        """No connection, no name looked up, no program started, nothing listens."""
        return (contacted() == 0 and not connects and not starts and not claude_calls() and not bridge.running()
                and bridge._server is None and not [n for n in lookups if n not in ("127.0.0.1", "localhost")])

    p = paths.params()
    names = [n for _k, n, _v in p.GetContents() or []]
    check("a new profile: no AI is chosen, nothing is allowed, the AI's code does not run without a question",
          backends.current() == "" and "Backend" not in names and consent.records() == [] and "Consent" not in names
          and approval.auto() is False and workbench.show_in_techdraw() is False, str(names))
    p.SetString("ProjectsDir", os.path.join(home, "projects"))       # never the user's own projects folder
    project = projects.create("Consent part")

    class Person:
        """The user at the screen: sees a question window, writes down the facts, answers."""

        def __init__(self):
            self.plan, self.seen, self.lost = [], [], []
            self._now = None
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._tick)
            self.timer.start(15)

        def will(self, *answers):
            """answers: "yes" or "no", one for each question that will come."""
            self.plan.extend(answers)

        def _tick(self):
            open_ = [w for w in QtWidgets.QApplication.topLevelWidgets()
                     if isinstance(w, ask_ui.Question) and not w.isHidden() and w.answer is None]
            if not open_ or open_[0] is self._now:
                return
            w = self._now = open_[0]
            self.seen.append({"headline": w.headline.text(), "text": w.text.text(),
                              "buttons": (w.yes_btn.text(), w.no_btn.text()), "modal": w.isModal(),
                              "default": [b.text() for b in (w.yes_btn, w.no_btn) if b.isDefault()],
                              "contacted": contacted(), "connects": len(connects), "starts": len(starts),
                              "claude": len(claude_calls()), "listens": bridge.running()})
            if self.plan:
                word = self.plan.pop(0)
            else:
                word = "no"
                self.lost.append(w.headline.text())      # a question nobody planned for
            (w.yes_btn if word == "yes" else w.no_btn).click()

    person = Person()

    def settings(change, pan_call=None):
        """Open the real dialog as the panel does, let `change` work in it, press OK."""
        def fake_exec(d):
            change(d)
            d._save()
            return d.result()
        SettingsDialog.exec = fake_exec
        try:
            (pan_call or pan._settings)()
        finally:
            del SettingsDialog.exec
        spin(150)

    def find(dlg, source):
        dlg.find_models(source)
        wait_for(lambda: dlg._asking is None, 10)

    def idle():
        return not pan._running() and pan.send_btn.text() == "Send"

    def send(text, seconds=30):
        pan.input.setPlainText(text)
        pan.send()
        return wait_for(idle, seconds)

    def notes(kind="note"):
        return [t for k, t in pan.entries if k == kind]

    # =====================================================================
    say("\n-- a new installation: three choices, and nothing is contacted --")
    panel.show()                             # the real dock in FreeCAD's window
    spin(500)
    pan = panel._dock.widget()
    ch = pan.chooser
    check("the panel shows the chooser, on screen, above the chat; the source line says 'No AI chosen'",
          panel._dock.isVisible() and ch.isVisible() and pan.isAncestorOf(ch) and pan.source.full == "No AI chosen",
          pan.source.full)
    check("the chooser has three plain choices with one line each",
          ch.TITLE == "Choose the AI that does the work"
          and [(n, ch.buttons[n].text(), ch.lines[n].text()) for n, _s, _l in ch.CHOICES] == [
              ("local", "Local model", "Local model on this or another computer (nothing leaves your network)"),
              ("apikey", "API key", "Your own Anthropic API key (your messages go to Anthropic)"),
              ("account", "Claude account", "Claude account (needs the Claude Code program)")]
          and ch.FOOT == "Nothing is contacted before you choose. You can change it later in Settings.",
          str([(n, ch.buttons[n].text(), ch.lines[n].text()) for n, _s, _l in ch.CHOICES]))
    check("opening the panel contacted nothing: no connection, no name looked up, no program started "
          "(`claude auth status` too), the server for Claude Code does not listen",
          nothing_left() and pan.run.proc is None and not pan.findChildren(QtCore.QProcess),
          f"{contacted()} {connects} {starts} {claude_calls()} {lookups}")
    check("... the claude program was not even looked for, and no setup message is shown",
          pan.claude is None and not [e for e in pan.entries if e[0].startswith("setup")], str(pan.entries))
    done = send("make a box")
    check("a message with no AI chosen: the panel says what to do, the text stays, nothing is contacted",
          done and pan.entries[-1] == ("error", "Choose the AI that does the work first: press one of the three "
                                                "buttons above.")
          and pan.input.toPlainText() == "make a box" and ("user", "make a box") not in pan.entries
          and nothing_left(), str(pan.entries[-1]))
    pan.input.clear()
    dlg = SettingsDialog(pan)
    shown = [w.text() for w in dlg.findChildren(QtWidgets.QLabel)]
    check("Settings of a new installation: no source is picked, and it says that nothing is contacted",
          dlg.backend() == "" and not [b for b in dlg.radios.values() if b.isChecked()]
          and dlg.hint.text() == "Choose who does the work. Nothing is contacted before you choose.", dlg.hint.text())
    check("Settings > Privacy: no service is allowed yet",
          dlg.privacy_rows == [] and PRIVACY_NONE in shown
          and PRIVACY_NONE == "No service outside this computer is allowed yet. CADprompt asks before it sends anything.",
          str(shown[-4:]))
    check("Settings has 'Also show the drawing tools in the TechDraw workbench', and it is off",
          dlg.techdraw.text() == "Also show the drawing tools in the TechDraw workbench" and not dlg.techdraw.isChecked())
    dlg.reject()
    dlg.deleteLater()
    check("... and looking at Settings contacted nothing", nothing_left())

    # =====================================================================
    say("\n-- a profile that has a source already keeps it, and is contacted by nothing before the yes --")
    p.SetString("claudePath", fake_claude)
    p.SetString("BaseUrl", "http://localhost:11434")
    backends.set_api_key(SAVED_KEY)
    p.SetString("LocalUrl", ROOM)
    p.SetString("LocalModel", "fake-model")
    labels = {"account": "Claude account", "apikey": "Anthropic API key · claude-opus-5-5",
              "local": "Local model · fake-model", "custom": "Other endpoint"}
    for source in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", source)
        one = panel.Panel()
        spin(400)                            # check_setup runs from a timer
        check(f"a profile with the source '{source}': the panel opens with it, no chooser, and nothing is "
              "contacted or started",
              backends.current() == source and one.source.full == labels[source] and one.chooser.isHidden()
              and nothing_left() and not person.seen, f"{one.source.full} {connects} {starts} {claude_calls()}")
        if source in ("account", "custom"):
            check(f"... '{source}': Claude Code is found on the disk but not started, also not to look at the "
                  "sign-in; the panel says that it asks first",
                  one.claude == fake_claude and [t for k, t in one.entries if k == "setup-note"] ==
                  ["CADprompt did not start Claude Code yet, also not to see whether you are signed in. "
                   "It asks you first, with your first message."], str(one.entries))
        one.deleteLater()
        spin(50)
    backends.set_api_key("")
    p.RemString("Backend")
    p.RemString("LocalUrl")
    p.RemString("LocalModel")
    check("(setup) back to a profile with no source", backends.current() == "" and nothing_left())

    # =====================================================================
    say("\n-- the choice 'Local model', a server on this computer: no question --")
    picked = []

    def choose_local(d):
        picked.append((d.backend(), d.local.isChecked()))
        d.local_url.setText(here.url)
        find(d, "local")
        picked.append((d.find_msg.text(), d.local_model.currentText()))
    settings(choose_local, ch.buttons["local"].click)
    check("a click on 'Local model' opens Settings with that source picked",
          picked[:1] == [("local", True)], str(picked))
    check("Find models for a server on this computer asks nothing and gets the list",
          picked[1:] == [("Found 1 model. Pick one from the list.", "fake-model")] and len(here.gets) == 1
          and not person.seen, str(picked))
    check("OK: the source is saved, the chooser is gone, no question came, nothing is in the list of "
          "allowed services",
          backends.current() == "local" and p.GetString("Backend", "") == "local" and ch.isHidden()
          and pan.source.full == "Local model · fake-model" and not person.seen and consent.records() == [],
          pan.source.full)
    here.add(answer("Hi."), answer("Hi again."))
    done = send("hello")
    local_note = "The model server runs on this computer: nothing leaves it, so CADprompt does not ask for a permission."
    check("the first message goes out without a question, and the panel says once that the server runs on "
          "this computer",
          done and len(here.requests) == 1 and ("claude", "Hi.") in pan.entries and notes().count(local_note) == 1
          and not person.seen, str(pan.entries))
    done = send("hello again")
    check("... the second message does not say it again",
          done and len(here.requests) == 2 and notes().count(local_note) == 1 and not person.seen)
    check("the other servers saw nothing, and no program was started",
          room.connections + room2.connections + anth.connections == 0 and not starts and not claude_calls())

    # =====================================================================
    say("\n-- a model server on another computer: asked once, before the first request --")
    p.SetString("LocalUrl", ROOM)
    pan._show_source()
    dlg = SettingsDialog(pan)
    n_seen, n_con, n_contact = len(person.seen), len(connects), contacted()
    person.will("no")
    find(dlg, "local")
    q = person.seen[-1] if len(person.seen) > n_seen else {}
    check("Find models for a server on another computer asks first, in a window of FreeCAD",
          len(person.seen) == n_seen + 1 and q.get("modal")
          and q.get("headline") == "May CADprompt send data to the model server at pc-in-the-room.test?", str(q))
    check("... the buttons are Allow and Not now, and Enter is Not now",
          q.get("buttons") == ("Allow", "Not now") and q.get("default") == ["Not now"], str(q))
    text = q.get("text", "")
    sent = ("your messages", "the results of the AI's code, and descriptions of the document (object names, sizes)",
            "images that you attach to a message", "the text of reference files that the AI reads")
    check("... the question says exactly what is sent, when, that an http:// connection is not encrypted, and "
          "that a proxy sees the name of the service",
          all("•  " + row in text for row in sent) and "the key of this server, when you saved one" in text
          and "This server is not on this computer." in text
          and "It is sent each time you send a message, and for each step of the AI." in text
          and "\"Find models\" sends only the key, when you saved one." in text
          and ROOM.startswith("http://") and consent.NOT_ENCRYPTED in text
          and "A proxy of this computer, when its settings name one, sees the name of the service." in text
          and "CADprompt asks once for each service. Settings > Privacy takes your yes back." in text, text)
    check("... while the question was open nothing had left: no connection, not even the name was looked up",
          q.get("contacted") == n_contact and contacted() == n_contact and q.get("connects") == n_con
          and "pc-in-the-room.test" not in lookups, f"{q} {lookups}")
    check("Not now: no connection to that server, the dialog says so, and the no is not kept",
          room.connections == 0 and len(connects) == n_con and "pc-in-the-room.test" not in lookups
          and dlg.find_msg.text() == NOT_ALLOWED and consent.records() == []
          and NOT_ALLOWED == "Nothing was sent: you did not allow this service. Press Find models again to be "
                             "asked again.", dlg.find_msg.text())
    person.will("yes")
    find(dlg, "local")
    today = datetime.date.today().isoformat()
    check("Allow: the list of models comes, with one request",
          dlg.find_msg.text() == "Found 1 model. Pick one from the list." and len(room.gets) == 1
          and room.connections == 1 and len(person.seen) == n_seen + 2, dlg.find_msg.text())
    check("... the yes is kept for this service: kind, host and the version of the list of data",
          consent.records() == [{"kind": "openai", "host": "pc-in-the-room.test", "data": consent.DATA_KINDS,
                                 "when": today, "clear": True}], str(consent.records()))
    row_text = lambda d: [w.text() for _k, _h, r, _b in d.privacy_rows for w in r.findChildren(QtWidgets.QLabel)]
    check("... and Settings > Privacy lists it at once, with a Forget button",
          [(k, h, b.text()) for k, h, _r, b in dlg.privacy_rows] == [("openai", "pc-in-the-room.test", "Forget")]
          and row_text(dlg) == ["Allowed: the model server at pc-in-the-room.test (since %s)" % today], str(row_text(dlg)))
    find(dlg, "local")
    check("Find models again: no question, the yes counts", len(person.seen) == n_seen + 2 and len(room.gets) == 2)
    dlg.privacy_rows[0][3].click()
    spin(50)
    check("Forget: the service is out of the list and out of the profile, at once",
          dlg.privacy_rows == [] and consent.records() == []
          and PRIVACY_NONE in [w.text() for w in dlg.findChildren(QtWidgets.QLabel)])
    dlg.reject()
    dlg.deleteLater()

    room.add(answer("From the room."), answer("Again from the room."))
    n_seen, n_con, n_room = len(person.seen), len(connects), room.connections
    person.will("no")
    done = send("hello room")
    not_now = ("Nothing was sent. CADprompt asks again with your next message. To work with another AI, open "
               "Settings and choose it: a local model on this computer needs no permission.")
    check("a message to the server on another computer asks first; Not now: nothing is sent",
          done and len(person.seen) == n_seen + 1 and room.connections == n_room and len(connects) == n_con
          and not room.requests, f"{len(person.seen) - n_seen} {room.connections - n_room}")
    check("... the panel says that nothing was sent and how to choose another AI; the message stays in the input",
          pan.entries[-1] == ("note", not_now) and consent.NOT_NOW == not_now
          and pan.input.toPlainText() == "hello room" and ("user", "hello room") not in pan.entries, str(pan.entries[-1]))
    n_contact = contacted()
    person.will("yes")
    done = send("hello room")
    q = person.seen[-1]
    check("the next message asks again; while the question is open nothing has left",
          len(person.seen) == n_seen + 2 and q["contacted"] == n_contact and q["connects"] == n_con, str(q))
    check("Allow: the message goes out, to that server and no other",
          done and len(room.requests) == 1 and room.connections > n_room and ("claude", "From the room.") in pan.entries
          and room2.connections + anth.connections == 0 and consent.allowed("openai", ROOM), str(pan.entries[-3:]))
    done = send("once more")
    check("the next message: no question", done and len(person.seen) == n_seen + 2 and len(room.requests) == 2)
    check("the key of the environment went nowhere", ENV_KEY not in json.dumps([s.requests + s.gets for s in servers]))

    p.SetString("LocalUrl", ROOM2)
    pan._show_source()
    n_seen = len(person.seen)
    person.will("no")
    done = send("hello other pc")
    check("the address changes to another host: asked again, for that host, and Not now sends nothing there",
          done and len(person.seen) == n_seen + 1
          and person.seen[-1]["headline"] == "May CADprompt send data to the model server at another-pc.test?"
          and room2.connections == 0 and "another-pc.test" not in lookups, str(person.seen[-1]))
    p.SetString("LocalUrl", ROOM)
    pan._show_source()
    dlg = SettingsDialog(pan)
    dlg.privacy_rows[0][3].click()
    dlg.reject()
    dlg.deleteLater()
    n_seen, n_room = len(person.seen), room.connections
    person.will("no")
    done = send("hello room, after Forget")
    check("after Forget the first host is asked for again, and Not now sends nothing",
          done and len(person.seen) == n_seen + 1 and room.connections == n_room and consent.records() == [])
    pan.input.clear()

    # =====================================================================
    say("\n-- the API key: nothing goes to the service before Allow, also not for Find models --")
    picked = []

    def choose_key(d):
        picked.append((d.backend(), d.apikey.isChecked()))
        d.api_model.setEditText("claude-fake-1")
    n_seen = len(person.seen)
    settings(choose_key, lambda: pan._choose("apikey"))
    check("the source 'API key' with no key saved: the panel asks for the key, and the key that is in the "
          "environment of the system (ANTHROPIC_API_KEY) is not used",
          picked == [("apikey", True)] and backends.current() == "apikey" and backends.api_key() == ""
          and backends.engine_config()["key"] == "" and len(person.seen) == n_seen
          and [t for k, t in pan.entries if k == "setup"] == ["No Anthropic API key is saved. Open Settings and "
                                                             "paste your key."], str(pan.entries))
    done = send("hello Anthropic")
    dlg = SettingsDialog(pan)
    find(dlg, "apikey")
    check("... a message and Find models send nothing then, and ask nothing",
          done and anth.connections == 0 and len(person.seen) == n_seen
          and dlg.api_find_msg.text() == "Enter your API key first.", dlg.api_find_msg.text())
    dlg.reject()
    dlg.deleteLater()
    pan.input.clear()

    n_seen, n_con = len(person.seen), len(connects)
    settings(lambda d: d.api_key.edit.setText(SAVED_KEY))
    check("the key is saved in Settings: the panel is ready, and nothing is sent or asked yet",
          backends.api_key() == SAVED_KEY and len(person.seen) == n_seen and anth.connections == 0
          and not [e for e in pan.entries if e[0].startswith("setup")], str(pan.entries))
    dlg = SettingsDialog(pan)
    person.will("no")
    find(dlg, "apikey")
    q = person.seen[-1] if len(person.seen) > n_seen else {}
    text = q.get("text", "")
    check("Find models before Allow: the question for the service comes, before anything is sent",
          len(person.seen) == n_seen + 1
          and q.get("headline") == "May CADprompt send data to Anthropic (api.anthropic.test)?"
          and q.get("buttons") == ("Allow", "Not now") and q.get("connects") == n_con, str(q))
    check("... it says what is sent, the key too, and names the proxy",
          all("•  " + row in text for row in sent) and "your API key, so that the service knows who asks" in text
          and "This service is not on this computer." in text and "\"Find models\" sends only the key." in text
          and "A proxy of this computer, when its settings name one, sees the name of the service." in text, text)
    check("Not now: no connection to the service, the key is not sent, and the dialog says so",
          anth.connections == 0 and not anth.gets and len(connects) == n_con and "api.anthropic.test" not in lookups
          and dlg.api_find_msg.text() == NOT_ALLOWED, dlg.api_find_msg.text())
    dlg.reject()
    dlg.deleteLater()
    anth.add(claude("Hello from the service."))
    person.will("no")
    done = send("hello Anthropic")
    check("a message before Allow: asked, Not now, nothing is sent",
          done and len(person.seen) == n_seen + 2 and anth.connections == 0 and not anth.requests
          and pan.entries[-1] == ("note", not_now) and consent.records() == [])
    person.will("yes")
    done = send("hello Anthropic")
    head = {k.lower(): v for k, v in (anth.requests[0]["headers"] if anth.requests else {}).items()}
    check("Allow: the message goes to the service, with the key from Settings and never the one of the environment",
          done and len(person.seen) == n_seen + 3 and len(anth.requests) == 1 and head.get("x-api-key") == SAVED_KEY
          and ("claude", "Hello from the service.") in pan.entries
          and ENV_KEY not in json.dumps([s.requests + s.gets for s in servers]), str(head))
    check("... the yes is kept for (anthropic, that host), and the other servers saw nothing new",
          [(r["kind"], r["host"], r["data"]) for r in consent.records()] ==
          [("anthropic", "api.anthropic.test", consent.DATA_KINDS)] and room2.connections == 0, str(consent.records()))
    dlg = SettingsDialog(pan)
    find(dlg, "apikey")
    check("Find models after Allow: no question, one request with the key",
          len(person.seen) == n_seen + 3 and len(anth.gets) == 1
          and {k.lower(): v for k, v in anth.gets[0]["headers"].items()}.get("x-api-key") == SAVED_KEY
          and dlg.api_find_msg.text() == "Found 2 models. Pick one from the list.", dlg.api_find_msg.text())
    check("Settings > Privacy names the service", row_text(dlg) ==
          ["Allowed: Anthropic (api.anthropic.test), with your API key (since %s)" % today], str(row_text(dlg)))
    dlg.reject()
    dlg.deleteLater()
    real_kinds, consent.DATA_KINDS = consent.DATA_KINDS, consent.DATA_KINDS + 1
    n_anth = anth.connections
    person.will("no")
    done = send("after an update that sends more")
    check("CADprompt starts to send another kind of data (DATA_KINDS): the old yes does not count, asked again",
          done and len(person.seen) == n_seen + 4 and anth.connections == n_anth, str(person.seen[-1]["headline"]))
    consent.DATA_KINDS = real_kinds
    pan.input.clear()

    # =====================================================================
    say("\n-- the Claude account: Claude Code is not started before Allow --")
    n_seen, n_claude, n_starts = len(person.seen), len(claude_calls()), len(starts)
    person.will("no")
    settings(lambda d: d.radios["account"].setChecked(True))
    q = person.seen[-1] if len(person.seen) > n_seen else {}
    text = q.get("text", "")
    check("the source 'Claude account' is chosen in Settings: with OK the question comes, before Claude Code "
          "is started for anything",
          backends.current() == "account" and len(person.seen) == n_seen + 1
          and q.get("headline") == "May CADprompt start Claude Code with your Claude account?"
          and q.get("buttons") == ("Allow", "Not now") and q.get("claude") == n_claude
          and q.get("starts") == n_starts and q.get("listens") is False, str(q))
    by_claude = ("your messages", "the results of the AI's code, and descriptions of the document (object names, sizes)",
                 "files of the project folder that the AI reads: images, reference files", "pictures of the 3D view")
    check("... it says that the Claude Code program is started with the login and sends the data to Anthropic, "
          "what the data is, and names the proxy",
          "The Claude Code program on this computer is started with your login and sends this data to Anthropic:" in text
          and all("•  " + row in text for row in by_claude)
          and "CADprompt also asks Claude Code whether you are signed in." in text
          and "Claude Code is a program of its own: it can make more connections, as its own settings say." in text
          and "A proxy of this computer, when its settings name one, sees the name of the service." in text, text)
    spin(400)
    check("Not now: `claude auth status` is not run, Claude Code is not started, nothing listens",
          len(claude_calls()) == n_claude and len(starts) == n_starts and not bridge.running()
          and pan.entries[-1] == ("note", not_now) and pan.claude == fake_claude, str(pan.entries[-2:]))

    # the doors themselves are locked too, for code that would not ask the panel
    answers, ends = [], []
    proc = runner.auth_status(fake_claude, answers.append, pan)
    lone = runner.ClaudeRun(pan)
    lone.finished.connect(lambda res: ends.append(dict(res)))
    lone.start(fake_claude, project, "hello", None, {})
    refused = []
    try:
        bridge.ensure(project, "account")
    except PermissionError as exc:
        refused.append(str(exc))
    spin(400)
    check("before the yes the doors are locked: auth_status() starts nothing, ClaudeRun.start() starts nothing "
          "and says why, bridge.ensure() refuses to listen",
          proc is None and not answers and not lone.running() and lone.proc is None
          and [e.get("result") for e in ends] == [runner.NOT_ALLOWED] and len(refused) == 1
          and len(claude_calls()) == n_claude and len(starts) == n_starts and not bridge.running(),
          f"{proc} {ends} {refused} {starts[n_starts:]}")

    person.will("no")
    done = send("hello Claude")
    spin(300)
    check("a message before Allow: asked, Not now: Claude Code is not started, not even for the sign-in",
          done and len(person.seen) == n_seen + 2 and len(claude_calls()) == n_claude and len(starts) == n_starts
          and not bridge.running() and pan.entries[-1] == ("note", not_now)
          and pan.input.toPlainText() == "hello Claude", str(pan.entries[-1]))
    person.will("yes")
    pan.send()
    wait_for(lambda: idle() and len([c for c in claude_calls() if c["argv"][:1] == ["-p"]]) > 0
             and len([c for c in claude_calls() if c["argv"][:2] == ["auth", "status"]]) > 0, 40)
    spin(300)
    q = person.seen[-1]
    calls = claude_calls()[n_claude:]
    runs = [c for c in calls if c["argv"][:1] == ["-p"]]
    check("Allow: while the question was open Claude Code had not been started and nothing listened",
          len(person.seen) == n_seen + 3 and q["claude"] == n_claude and q["starts"] == n_starts
          and q["listens"] is False, str(q))
    check("... then Claude Code is asked for the sign-in and gets the message",
          len(runs) == 1 and runs[0]["stdin"] == "hello Claude"
          and len([c for c in calls if c["argv"][:2] == ["auth", "status"]]) >= 1
          and ("claude", "fake answer to: hello Claude") in pan.entries, str([c["argv"][:2] for c in calls]))
    check("... with the login of Claude Code, not with a key of the environment",
          bool(runs) and "ANTHROPIC_API_KEY" not in runs[0]["env"] and "ANTHROPIC_AUTH_TOKEN" not in runs[0]["env"],
          str(runs[0]["env"] if runs else None))
    check("... the yes is kept for the service (account, the host of Anthropic), the bridge listens for Claude Code",
          ("account", "api.anthropic.test") in [(r["kind"], r["host"]) for r in consent.records()]
          and backends.service("account") == ("account", backends.ANTHROPIC_URL) and bridge.running(),
          str(consent.records()))
    n_seen2, n_runs = len(person.seen), len([c for c in claude_calls() if c["argv"][:1] == ["-p"]])
    pan.send("once more")
    wait_for(lambda: idle() and len([c for c in claude_calls() if c["argv"][:1] == ["-p"]]) > n_runs, 40)
    check("the next message: no question", len(person.seen) == n_seen2
          and len([c for c in claude_calls() if c["argv"][:1] == ["-p"]]) == n_runs + 1)

    n_seen, n_claude = len(person.seen), len(claude_calls())
    names = []

    def forget_account(d):
        names.extend(row_text(d))
        [b for k, _h, _r, b in d.privacy_rows if k == "account"][0].click()
    settings(forget_account)
    spin(300)
    check("Settings > Privacy names the service, and Forget takes the yes back: the bridge stops listening at "
          "once, and no question comes by itself",
          "Allowed: Anthropic, through Claude Code with your Claude account (since %s)" % today in names
          and not [r for r in consent.records() if r["kind"] == "account"] and not bridge.running()
          and len(person.seen) == n_seen and len(claude_calls()) == n_claude, f"{names} {consent.records()}")
    person.will("no")
    done = send("hello Claude, after Forget")
    spin(300)
    check("... the next message asks again, and Not now starts nothing",
          done and len(person.seen) == n_seen + 1 and len(claude_calls()) == n_claude and not bridge.running())
    pan.input.clear()

    # Forget while Claude Code works: the program sends data until it ends, so it is stopped
    person.will("yes")
    n_slow = len([c for c in claude_calls() if c["stdin"].startswith("slow")])
    pan.send("slow: think for a minute")
    wait_for(lambda: len([c for c in claude_calls() if c["stdin"].startswith("slow")]) > n_slow
             and bridge.running(), 30)
    at_work = pan.run.running() and bridge.running() and pan.send_btn.text() == "Stop"
    seen = {}

    def forget_at_work(d):
        [b for k, _h, _r, b in d.privacy_rows if k == "account"][0].click()
        seen["gate"] = bool(bridge._host._active)        # closed at once, before the program is gone
        seen["ended"] = wait_for(lambda: not pan.run.running() and not bridge.running(), 15)
        seen["note"] = pan.entries[-1]                   # OK shows the conversation again, without it
    settings(forget_at_work)
    check("Forget while Claude Code works on a request: no tool call runs any more at once, the program is "
          "ended and the bridge stops listening while Settings is still open, and the chat says why",
          at_work and seen.get("gate") is False and seen.get("ended") is True and idle()
          and not [r for r in consent.records() if r["kind"] == "account"]
          and seen.get("note") == ("note", panel.FORGOT), f"{at_work} {seen}")
    pan.input.clear()

    # the same, and the dialog is closed with Cancel: Forget is not a setting that Cancel takes back
    person.will("yes")
    n_slow = len([c for c in claude_calls() if c["stdin"].startswith("slow")])
    pan.send("slow: think for a minute more")
    wait_for(lambda: len([c for c in claude_calls() if c["stdin"].startswith("slow")]) > n_slow
             and bridge.running(), 30)
    at_work = pan.run.running() and bridge.running() and pan.send_btn.text() == "Stop"

    def forget_and_cancel(d):
        [b for k, _h, _r, b in d.privacy_rows if k == "account"][0].click()
        d.reject()
        return d.result()
    SettingsDialog.exec = forget_and_cancel
    try:
        pan._settings()
    finally:
        del SettingsDialog.exec
    gate = bool(bridge._host._active)
    ended = wait_for(lambda: not pan.run.running() and not bridge.running(), 15)
    check("Forget, then Cancel, while Claude Code works: the same: stopped, nothing listens, the chat says why "
          "and the button says Send again",
          at_work and gate is False and ended and idle() and pan.entries[-1] == ("note", panel.FORGOT)
          and pan.status.text() == "" and not [r for r in consent.records() if r["kind"] == "account"],
          f"{at_work} {gate} {ended} {pan.entries[-2:]}")
    pan.input.clear()

    p.SetString("Backend", "custom")         # BaseUrl is http://localhost:11434
    pan._show_source()
    pan.check_setup()
    n_seen, n_claude = len(person.seen), len(claude_calls())
    person.will("no")
    done = send("hello endpoint")
    spin(300)
    q = person.seen[-1] if len(person.seen) > n_seen else {}
    check("Claude Code with another endpoint is another service: asked for, also when the server is on this "
          "computer, because Claude Code is a program of its own",
          done and q.get("headline") == "May CADprompt start Claude Code for the server at localhost (this computer)?"
          and "the endpoint token from Settings" in q.get("text", "") and len(claude_calls()) == n_claude
          and not bridge.running(), f"{len(claude_calls()) - n_claude} {bridge.running()} {q}")
    pan.input.clear()

    # =====================================================================
    say("\n-- Settings: the drawing tools in TechDraw are the user's own choice --")
    p.SetString("Backend", "local")          # a source that asks nothing when Settings is closed with OK
    p.SetString("LocalUrl", here.url)
    pan._show_source()
    settings(lambda d: d.techdraw.click())
    check("the checkbox on, OK: saved (ShowInTechDraw), and the drawing commands are there for TechDraw",
          workbench.show_in_techdraw() is True and p.GetBool("ShowInTechDraw", False) is True
          and workbench._manipulator is not None
          and all(Gui.Command.get(name) is not None for name in workbench.DRAWING))
    dlg = SettingsDialog(pan)
    check("Settings shows it on", dlg.techdraw.isChecked())
    dlg.techdraw.click()
    dlg.reject()
    dlg.deleteLater()
    check("Cancel changes nothing", workbench.show_in_techdraw() is True)
    settings(lambda d: d.techdraw.click())
    check("the checkbox off, OK: saved, and the buttons leave TechDraw",
          workbench.show_in_techdraw() is False and p.GetBool("ShowInTechDraw", True) is False
          and workbench._manipulator is None)

    check("the real Anthropic was never looked up or contacted in all of this",
          "api.anthropic.com" not in lookups and not [a for a in connects if "anthropic.com" in a], str(lookups))
    check("every question had its answer planned", not person.lost and not person.plan, f"{person.lost} {person.plan}")
    person.timer.stop()
    bridge.stop()
    socket.getaddrinfo = real_getaddrinfo
    for srv in servers:
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
