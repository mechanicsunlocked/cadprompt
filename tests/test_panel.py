# RUN: freecad-gui
"""Tests for the panel and the settings dialog with all four AI sources.

Run in the real FreeCAD GUI without a display, with a throw-away profile so no
real setting is touched:

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_panel.py

Nothing real is called: the local engine is panel_stub_engine.py, `claude` is
panel_fake_claude.py, the bridge is a two-line stand-in and every key is made up.
"""

import faulthandler
import http.server
import json
import os
import shutil
import stat
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

from utf8_output import utf8_output            # noqa: E402

utf8_output()

OUT = sys.__stdout__             # FreeCAD's own sys.stdout goes to its report view
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_panel.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []                 # exceptions inside Qt slots do not reach us any other way


def say(text):
    try:                         # FreeCAD's window program can be without any output (Windows)
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


class _Fake(http.server.BaseHTTPRequestHandler):
    """A pretend model server for 'Find models'."""
    seen = []                    # (port, path, Authorization header) of every request

    def log_message(self, *a):
        pass

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Fake.seen.append((self.server.server_address[1], self.path, self.headers.get("Authorization")))
        try:
            if self.path == "/v1/models":
                self._json({"data": [{"id": "google/gemma-4-e4b"}, {"id": "other-model"}]})
            elif self.path == "/slow/models":
                time.sleep(2)
                self._json({"data": [{"id": "slow-model"}]})
            elif self.path == "/locked/models":
                self.send_response(401)
                self.end_headers()
            elif self.path == "/drip/models":      # one byte a second: the socket timeout never trips
                self.send_response(200)
                self.end_headers()
                for _ in range(12):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(1)
            else:
                self._json({"error": "Unexpected endpoint or method."})
        except OSError:
            pass


def main():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtGui, QtWidgets

    # Without a display Qt warns "This plugin does not support raise()". FreeCAD 1.1 sends that
    # warning to its notification area while the area is busy showing itself, and locks up.
    QtCore.QLoggingCategory.setFilterRules("default.warning=false")

    def spin(ms=50):
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_for(cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            spin(20)
        return bool(cond())

    home = os.environ["FREECAD_USER_HOME"]
    say("FreeCAD user data: " + App.getUserAppDataDir())

    # ---- a pretend claude, a pretend uvx, the stub engine, a pretend bridge ----
    # In a folder with a space and a letter that is not ASCII, as in C:\\Users\\Zoë Müller.
    bindir = os.path.join(home, "bin of Zoë")
    try:
        os.makedirs(bindir)
    except (OSError, ValueError) as exc:
        check("(setup) a folder name with a space and a letter that is not ASCII can be made", False, repr(exc))
        bindir = os.path.join(home, "bin of Zoe")
        os.makedirs(bindir)
    python = os.environ.get("CADPROMPT_TEST_PYTHON") or shutil.which("python3") or shutil.which("python") or "python3"
    script = os.path.join(HERE, "panel_fake_claude.py")
    if os.name == "nt":
        # what `npm install -g` makes on Windows: a .cmd file, which only cmd.exe can run
        fake_claude = os.path.join(bindir, "claude.cmd")
        with open(fake_claude, "w") as f:
            f.write('@echo off\n"%s" "%s" %%*\n' % (python, script))
    else:
        fake_claude = os.path.join(bindir, "claude")
        with open(fake_claude, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (python, script))
        os.chmod(fake_claude, 0o755)
    fake_uvx = os.path.join(bindir, "uvx")
    open(fake_uvx, "w").close()
    # What the start script of a FreeCAD bundle (AppImage, macOS app) leaves in the
    # environment. Nothing of it may reach a program that the addon starts.
    bundle = os.path.join(home, "pretend bundle")
    os.makedirs(os.path.join(bundle, "usr", "bin"))
    os.environ["PYTHONPATH"] = os.path.join(bundle, "usr", "lib")
    os.environ["PANEL_BUNDLE_CERT"] = os.path.join(bundle, "usr", "ssl", "cacert.pem")
    os.environ["PANEL_BUNDLE_OTHER"] = "kept"
    os.environ["PATH"] = os.pathsep.join([os.path.join(bundle, "usr", "bin"), os.environ.get("PATH", "")])
    claude_log = os.path.join(home, "fake-claude.jsonl")
    os.environ["PANEL_FAKE_CLAUDE_LOG"] = claude_log
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(home, "claude-config")
    # what a user could have in the shell: none of it may reach Claude Code unasked
    os.environ["ANTHROPIC_API_KEY"] = "shell-key-must-not-leak"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "shell-token-must-not-leak"
    os.environ["ANTHROPIC_BASE_URL"] = "http://shell.invalid"

    def claude_calls():
        try:
            with open(claude_log, encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    import panel_stub_engine as stub
    import cadprompt
    sys.modules["cadprompt.local_engine"] = stub
    cadprompt.local_engine = stub
    from cadprompt import backends, bridge, panel, paths, projects, runner, settings_dialog
    from cadprompt.settings_dialog import SettingsDialog

    real_roots = paths.bundle_roots()
    say("FreeCAD's own bundle: " + (", ".join(real_roots) or "none (a FreeCAD of the system)"))
    paths.bundle_roots = lambda: real_roots + [bundle]

    bridge_calls = []
    bridge_ok = [True]

    def fake_ensure():
        bridge_calls.append(1)
        if not bridge_ok[0]:
            raise RuntimeError("the test turned the bridge off")
        return "test-token", "bridge running"
    bridge.ensure = fake_ensure
    asked = []
    QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a) or QtWidgets.QMessageBox.No)

    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(home, "projects"))
    p.SetString("claudePath", fake_claude)
    p.SetString("uvxPath", fake_uvx)
    project = projects.create("Test part")
    other = projects.create("Other part")
    os.utime(project, None)      # the newest project is the one the panel opens
    check("test setup: two projects from the template", os.path.isfile(os.path.join(project, "CLAUDE.md"))
          and os.path.isdir(other) and paths.find_tool("claude") == fake_claude)

    def kinds(pan, kind):
        return [t for k, t in pan.entries if k == kind]

    def setup_errors(pan):
        return kinds(pan, "setup")

    panels = []

    def new_panel(settle=150):
        pan = panel.Panel()
        panels.append(pan)
        spin(settle)             # check_setup runs from a timer
        return pan

    # =====================================================================
    say("\n-- settings dialog --")
    for b in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", b)
        dlg = SettingsDialog()
        see = lambda w, dlg=dlg: w.isVisibleTo(dlg)
        check(f"dialog opens with {b} chosen", dlg.backend() == b and dlg.radios[b].isChecked())
        check(f"{b}: only its own fields are shown",
              (see(dlg.apikey_box), see(dlg.local_box), see(dlg.custom_box)) ==
              (b == "apikey", b == "local", b == "custom"),
              str((see(dlg.apikey_box), see(dlg.local_box), see(dlg.custom_box))))
        claude_fields = [see(w) for w in (dlg.web, dlg.claude, dlg.uvx)]
        check(f"{b}: web toggle, claude and uvx program " + ("hidden" if b == "local" else "shown"),
              claude_fields == [b != "local"] * 3, str(claude_fields))
        check(f"{b}: Claude model " + ("shown" if b in ("account", "apikey") else "hidden"),
              see(dlg.model_override) == (b in ("account", "apikey")))
        check(f"{b}: projects folder is always there", see(dlg.projects))
        dlg.deleteLater()

    p.SetString("Backend", "account")
    dlg = SettingsDialog()
    see = lambda w: w.isVisibleTo(dlg)
    labels = [dlg.radios[b].text() for b in ("account", "apikey", "local", "custom")]
    check("the four choices are named in plain words", labels == [
        "Claude account (signed in to Claude Code)", "Anthropic API key",
        "Local model or other server (LM Studio, Ollama, OpenAI-compatible)",
        "Claude Code with another endpoint"], str(labels))
    dlg.local.click()
    check("clicking 'Local model' swaps the fields at once",
          see(dlg.local_box) and not see(dlg.claude_box) and not see(dlg.apikey_box))
    check("local: address is pre-filled with LM Studio's, model is an editable list",
          dlg.local_url.text() == "http://127.0.0.1:1234/v1" and dlg.local_model.isEditable()
          and dlg.local_model.currentText() == "")
    dlg.apikey.click()
    check("clicking 'Anthropic API key' swaps again",
          see(dlg.apikey_box) and not see(dlg.local_box) and see(dlg.claude_box))
    check("API key field hides what is typed and says nothing is saved yet",
          dlg.api_key.edit.echoMode() == QtWidgets.QLineEdit.Password
          and dlg.api_key.edit.placeholderText() != "saved" and not dlg.api_key.drop.isEnabled())
    dlg.api_key.edit.setText("sk-ant-made-up-1")
    dlg.local.click()
    dlg.local_url.setText("http://127.0.0.1:1/v1")
    dlg.local_model.setEditText("some-model")
    dlg.local_key.edit.setText("made-up-local-key")
    dlg.projects.setText(os.path.join(home, "projects"))
    dlg.web.setChecked(True)
    dlg.model_override.setText("sonnet")
    dlg.url.setText("http://localhost:11434")
    dlg.model.setText("endpoint-model")
    dlg.token.setText("endpoint-token")
    dlg._save()
    check("OK saves the source and the local server", (p.GetString("Backend"), p.GetString("LocalUrl"),
          p.GetString("LocalModel")) == ("local", "http://127.0.0.1:1/v1", "some-model"))
    check("OK saves the old fields too", (p.GetBool("AllowWeb"), p.GetString("ModelOverride"),
          p.GetString("BaseUrl"), p.GetString("Model"), p.GetString("AuthToken"), p.GetString("claudePath"),
          p.GetString("uvxPath")) == (True, "sonnet", "http://localhost:11434", "endpoint-model",
                                      "endpoint-token", fake_claude, fake_uvx))
    key_file = os.path.join(App.getUserAppDataDir(), "cadprompt_api_key")
    check("OK saves both keys in their own files" + (", mode 0600" if os.name == "posix" else ""),
          backends.api_key() == "sk-ant-made-up-1" and backends.local_key() == "made-up-local-key"
          and (os.name != "posix" or stat.S_IMODE(os.stat(key_file).st_mode) == 0o600))
    check("no key went into FreeCAD's parameters",
          not [v for (_k, _n, v) in p.GetContents() if "made-up" in str(v)])
    check("OK closed the dialog as accepted", dlg.result() == QtWidgets.QDialog.Accepted)
    dlg.deleteLater()

    dlg = SettingsDialog()
    check("a saved key is never loaded back: empty field, placeholder 'saved'",
          dlg.api_key.edit.text() == "" and dlg.api_key.edit.placeholderText() == "saved"
          and dlg.local_key.edit.text() == "" and dlg.local_key.edit.placeholderText() == "saved")
    dlg._save()
    check("OK with the key fields left empty keeps the keys",
          backends.api_key() == "sk-ant-made-up-1" and backends.local_key() == "made-up-local-key")
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.api_key.edit.setText("sk-ant-made-up-2")
    dlg.local_key.drop.click()
    check("Remove only marks the key: nothing is deleted before OK", backends.local_key() == "made-up-local-key"
          and dlg.local_key.edit.placeholderText() != "saved")
    dlg._save()
    check("OK replaces a key that was typed and deletes a key marked with Remove",
          backends.api_key() == "sk-ant-made-up-2" and backends.local_key() == ""
          and not os.path.exists(os.path.join(App.getUserAppDataDir(), "cadprompt_local_key")))
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.api_key.drop.click()
    dlg.reject()
    check("Cancel keeps everything", backends.api_key() == "sk-ant-made-up-2" and p.GetString("Backend") == "local")
    dlg.deleteLater()

    # ---- Find models ----
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    ticks = [0]
    ticker = QtCore.QTimer()
    ticker.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    ticker.start(50)

    def find(dlg, url, seconds=10):
        """Press Find models; (seconds the click took, seconds until the answer, GUI ticks meanwhile)."""
        dlg.local_url.setText(url)
        n, t0 = ticks[0], time.time()
        dlg.find.click()
        click = time.time() - t0
        wait_for(lambda: dlg._asking is None, seconds)
        return click, time.time() - t0, ticks[0] - n

    dlg = SettingsDialog()
    dlg.local_model.setEditText("")
    click, total, _ = find(dlg, base + "/v1")
    items = [dlg.local_model.itemText(i) for i in range(dlg.local_model.count())]
    check("Find models fills the list from the server", items == ["google/gemma-4-e4b", "other-model"], str(items))
    check("Find models: with no model typed, the first one is chosen",
          dlg.local_model.currentText() == "google/gemma-4-e4b")
    check("Find models says how many it found", dlg.find_msg.text().startswith("Found 2 models"),
          dlg.find_msg.text())
    dlg.local_model.setEditText("other-model")
    find(dlg, base + "/v1")
    check("Find models keeps the model that was chosen", dlg.local_model.currentText() == "other-model")
    dlg.local_model.setEditText("typed-by-hand")
    find(dlg, base + "/v1")
    check("Find models keeps a name typed by hand that the server does not list",
          dlg.local_model.currentText() == "typed-by-hand" and dlg.local_model.count() == 2)

    click, total, n = find(dlg, base + "/slow")
    check("slow server (2 s): the click returns at once", click < 0.3, f"{click:.2f} s")
    check("slow server: FreeCAD keeps running while it waits", n >= 20 and 1.8 < total < 4, f"{n} ticks, {total:.1f} s")
    check("slow server: the answer still arrives", dlg.local_model.itemText(0) == "slow-model"
          and dlg.find.isEnabled())

    click, total, n = find(dlg, "http://127.0.0.1:1/v1")
    check("dead port: a short plain message, button usable again",
          dlg.find_msg.text() == "No answer from the server. Is it running, and is the address right?"
          and dlg.find.isEnabled() and total < 6, f"{dlg.find_msg.text()!r} after {total:.1f} s")
    check("dead port: the list is left alone", dlg.local_model.itemText(0) == "slow-model")
    find(dlg, base + "/locked")
    check("refused key: says so", dlg.find_msg.text() == "The server did not accept the key.", dlg.find_msg.text())
    find(dlg, base)
    check("wrong address (no /v1): says to check the address", "/v1" in dlg.find_msg.text()
          and "Traceback" not in dlg.find_msg.text(), dlg.find_msg.text())
    find(dlg, "")
    check("empty address: asks for it, does not start", "Enter the server address" in dlg.find_msg.text())
    click, total, n = find(dlg, base + "/drip", seconds=12)
    check("server that never finishes: given up after about 6 s, FreeCAD kept running",
          5 < total < 8 and n >= 60 and dlg.find.isEnabled() and dlg.find_msg.text().startswith("No answer"),
          f"{total:.1f} s, {n} ticks, {dlg.find_msg.text()!r}")
    n = len(_Fake.seen)
    find(dlg, "127.0.0.1:%d/v1" % server.server_address[1])
    check("address without http://: says what is missing, asks nothing",
          dlg.find_msg.text() == "Start the address with http:// or https://, for example http://127.0.0.1:1234/v1"
          and len(_Fake.seen) == n and dlg._asking is None, dlg.find_msg.text())
    dlg.reject()
    dlg.deleteLater()

    # ---- the saved key is for the saved address only ----
    server_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server_b.serve_forever, daemon=True).start()
    port_a, port_b = server.server_address[1], server_b.server_address[1]
    base_b = "http://127.0.0.1:%d" % port_b
    dlg = SettingsDialog()
    dlg.local_url.setText(base + "/v1")
    dlg.local_key.edit.setText("KEY-FOR-A")
    dlg._save()
    dlg.deleteLater()
    dlg = SettingsDialog()
    check("the key of server A is saved", backends.local_config()["key"] == "KEY-FOR-A"
          and dlg.local_key.edit.placeholderText() == "saved" and dlg.local_key.drop.isEnabled())
    find(dlg, base + "/locked")
    check("Find models sends the saved key to its own server, on any path",
          _Fake.seen[-1] == (port_a, "/locked/models", "Bearer KEY-FOR-A"), str(_Fake.seen[-1]))
    dlg.local_url.setText(base_b + "/v1")
    check("another address typed: the key row does not say 'saved' any more",
          dlg.local_key.edit.placeholderText() != "saved" and not dlg.local_key.drop.isEnabled())
    find(dlg, base_b + "/v1")
    check("Find models does not send the saved key to another server",
          _Fake.seen[-1] == (port_b, "/v1/models", None) and dlg.find_msg.text().startswith("Found 2"),
          str(_Fake.seen[-1]))
    dlg.local_url.setText(base + "/v1")
    check("back on the saved address: 'saved' again",
          dlg.local_key.edit.placeholderText() == "saved" and dlg.local_key.drop.isEnabled())
    dlg.local_url.setText(base_b + "/v1")
    dlg._save()
    check("OK with a new address and no new key: the old key does not go to the new server",
          backends.local_config()["url"] == base_b + "/v1" and backends.local_config()["key"] == ""
          and backends.local_key() == "", str(backends.local_config()))
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.local_url.setText(base + "/v1")
    dlg.local_key.edit.setText("KEY-FOR-A-2")
    dlg._save()
    dlg.deleteLater()
    check("a new address with a new key keeps that key", backends.local_config()["key"] == "KEY-FOR-A-2")
    dlg = SettingsDialog()
    dlg.local_url.setText(base + "/other/v1")
    dlg._save()
    dlg.deleteLater()
    check("another path on the same server keeps the key", backends.local_config() ==
          {"url": base + "/other/v1", "model": backends.local_config()["model"], "key": "KEY-FOR-A-2"},
          str(backends.local_config()))
    backends.set_local_key("")
    server_b.shutdown()
    server_b.server_close()
    ticker.stop()
    server.shutdown()

    # =====================================================================
    say("\n-- panel: opens with every source --")
    for k in ("LocalUrl", "LocalModel"):
        p.RemString(k)
    p.SetBool("AllowWeb", False)
    for b in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", b)
        before = len(SLOT_ERRORS)
        try:
            pan = new_panel()
            ok = True
        except Exception:
            ok = False
            SLOT_ERRORS.append(traceback.format_exc())
        check(f"Panel() with Backend={b}", ok and len(SLOT_ERRORS) == before, "\n".join(SLOT_ERRORS[before:]))
        if ok:
            check(f"{b}: the active source is shown: '{pan.source.full}'",
                  pan.source.full.startswith(backends.label(b)))
            check(f"{b}: the project and its first note are there",
                  pan.project == project and pan.entries[0] == ("note", "Project **test-part**."))
    p.SetString("Backend", "nonsense")
    pan = new_panel()
    check("an unknown Backend value opens as the Claude account", pan.source.full == "Claude account · sonnet",
          pan.source.full)

    # =====================================================================
    say("\n-- panel: local model --")
    p.SetString("Backend", "local")
    p.SetString("LocalModel", "")
    del stub.calls[:]
    real_find_tool = paths.find_tool
    paths.find_tool = lambda name: None      # no Claude Code and no uv on this machine
    bridge_ok[0] = False                     # and no bridge
    n_bridge, n_claude = len(bridge_calls), len(claude_calls())
    pan = new_panel()
    check("local without a model: one setup message, it points to Settings",
          len(setup_errors(pan)) == 1 and "Settings" in setup_errors(pan)[0]
          and "Find models" in setup_errors(pan)[0], str(setup_errors(pan)))
    check("local: no word about Claude Code, uv or the bridge",
          not [t for t in setup_errors(pan) if "Claude" in t or "uv " in t or "bridge" in t])
    pan.send("make a box")
    spin(50)
    check("local without a model: nothing is sent, the message is not doubled",
          not [c for c in stub.calls if c[0] == "start"] and len(setup_errors(pan)) == 1
          and not kinds(pan, "user"))
    p.SetString("LocalUrl", "")
    p.SetString("LocalModel", "google/gemma-4-e4b")
    check("local without a server address: says so, the old message is gone",
          pan.check_setup() is False and len(setup_errors(pan)) == 1 and "server address" in setup_errors(pan)[0],
          str(setup_errors(pan)))
    p.SetString("LocalUrl", "http://127.0.0.1:1234/v1")
    check("local with address and model: ready, the setup messages are gone",
          pan.check_setup() is True and not setup_errors(pan))
    real_flatpak = paths.in_flatpak
    paths.in_flatpak = lambda: True
    check("local works in a Flatpak", pan.check_setup() is True and not setup_errors(pan))
    paths.in_flatpak = real_flatpak
    check("local: Claude Code, uv and the bridge were never looked at",
          len(bridge_calls) == n_bridge and len(claude_calls()) == n_claude and not asked and pan.claude is None)

    starts = []                              # ClaudeRun.start must not be reached from here on
    real_start = runner.ClaudeRun.start
    runner.ClaudeRun.start = lambda self, *a, **k: starts.append((a, k))
    projects.set_session(project, "claude-session-keep")
    stub.replay[:] = [("tool", "run_python", "box = doc.addObject('Part::Box', 'Box')"),
                      ("text", "A box 10 x 20 x 30 mm."),
                      ("finished", {"is_error": False, "result": "A box 10 x 20 x 30 mm."})]
    pan.input.setPlainText("make a box 10 x 20 x 30")
    pan.send()
    check("local: send() goes to LocalRun.start(project, text, local_config())",
          [c for c in stub.calls if c[0] == "start"] == [("start", project, "make a box 10 x 20 x 30",
           {"url": "http://127.0.0.1:1234/v1", "model": "google/gemma-4-e4b", "key": ""})],
          str(stub.calls))
    check("local: ClaudeRun.start is not called, the bridge is not asked",
          not starts and len(bridge_calls) == n_bridge)
    check("local: while it works: 'The local model is working…', the button says Stop, the input is empty",
          pan.status.text() == "The local model is working…" and pan.send_btn.text() == "Stop"
          and pan.input.toPlainText() == "", pan.status.text())
    pan.send("second message while busy")
    check("local: a second message while busy is ignored", len([c for c in stub.calls if c[0] == "start"]) == 1)
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: the entries appear: you, tool, answer", pan.entries[-3:] == [
        ("user", "make a box 10 x 20 x 30"), ("tool", "`run_python` box = doc.addObject('Part::Box', 'Box')"),
        ("claude", "A box 10 x 20 x 30 mm.")], str(pan.entries[-3:]))
    check("local: when done the button says Send and the status is empty",
          pan.send_btn.text() == "Send" and pan.status.text() == "")
    check("local: the Claude Code session file is not touched", projects.get_session(project) == "claude-session-keep")
    shown = pan.view.toPlainText()
    check("local: the view shows the conversation", "make a box 10 x 20 x 30" in shown and "A box 10 x 20 x 30 mm." in shown)

    stub.replay[:] = [("finished", {"is_error": False, "result": "ok", "session_id": "from-a-broken-engine"})]
    pan.send("again")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: even a session_id in the result is not stored",
          projects.get_session(project) == "claude-session-keep"
          and open(os.path.join(project, ".cadprompt", "session")).read().strip() == "claude-session-keep")

    stub.replay[:] = [("tool", "run_python", "x"), ("tool_error", "run_python")]
    pan.send("break it")
    spin(100)
    check("local: status names the tool: " + pan.status.text(),
          pan.status.text() == "The local model is working… (run_python)")
    check("local: a failed tool: 'the local model is looking at why'",
          kinds(pan, "error")[-1] == "`run_python` failed, the local model is looking at why", str(kinds(pan, "error")))
    n_err = len(kinds(pan, "error"))
    pan.send_btn.click()                     # Stop
    spin(50)
    check("local: Stop calls LocalRun.stop()", stub.calls[-1] == ("stop",), str(stub.calls[-1]))
    check("local: stopping is quiet: a note, no error, button back to Send",
          pan.entries[-1] == ("note", "Stopped.") and len(kinds(pan, "error")) == n_err
          and pan.send_btn.text() == "Send" and pan.status.text() == "", str(pan.entries[-1]))

    for res, want in (
        ({"is_error": True, "result": "The model server at http://127.0.0.1:1234/v1 does not answer. Is it running?"},
         "The model server at http://127.0.0.1:1234/v1 does not answer. Is it running?"),
        ({"is_error": True, "result": ""}, "The local model stopped without an answer."),
    ):
        stub.replay[:] = [("finished", res)]
        pan.send("hello")
        wait_for(lambda: pan.send_btn.text() == "Send", 3)
        check(f"local error is shown as it is: '{want[:40]}…'", pan.entries[-1] == ("error", want), str(pan.entries[-1]))
    check("local: no error text says 'Claude'", not [t for t in kinds(pan, "error") if "Claude" in t],
          str(kinds(pan, "error")))
    stub.replay[:] = [("finished", {"is_error": True, "result": "please login: authentication failed"})]
    pan.send("hello")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: no 'run claude and sign in' hint on a local error",
          pan.entries[-1] == ("error", "please login: authentication failed"), str(pan.entries[-1]))

    # buttons: the same slash commands for every source
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    buttons = {b.text(): b for b in pan.findChildren(QtWidgets.QPushButton)}
    buttons["Check"].click()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: Check sends /check", stub.calls[-1][:3] == ("start", project, "/check"), str(stub.calls[-1]))
    sizes = {a.text(): a for a in buttons["Drawing"].menu().actions()}
    sizes["A3"].trigger()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: Drawing > A3 sends /drawing A3", list(sizes) == ["A2", "A3", "A4", "A5"]
          and stub.calls[-1][:3] == ("start", project, "/drawing A3"), str(stub.calls[-1]))

    # modes: the engine writes the mode file for /teach, the panel follows it
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    projects.set_mode(project, "teach")       # what the engine does for "/teach"
    pan.send("/teach")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: after /teach the Teach button is on", pan.teach_btn.isChecked() and not pan.build_btn.isChecked())
    pan.build_btn.click()
    check("mode buttons still write the mode file", projects.get_mode(project) == "build")

    # references
    ref = os.path.join(home, "sketch.txt")
    open(ref, "w").write("flange, 6 holes\n")
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(ref)])
    took = pan.take_mime(mime)
    refs = [f for f in os.listdir(os.path.join(project, "refs")) if f.endswith("sketch.txt")]
    check("local: a dropped file goes to refs/ and the note names the local model",
          took and len(refs) == 1 and pan.entries[-1][1].endswith("Tell the local model what to do with them."),
          str(pan.entries[-1]))
    check("local: tooltips name the local model", pan.build_btn.toolTip() == "The local model builds the whole part"
          and pan.refs_btn.toolTip().endswith("for the local model"))

    # history and New chat
    stub.past[project] = [("user", "earlier question"), ("tool", "`run_python` x"), ("claude", "earlier answer")]
    del stub.calls[:]
    pan._select("test-part", open_model=False)
    check("local: the earlier conversation comes from local_engine.history(project)",
          ("history", project) in stub.calls and pan.entries == [
              ("note", "Project **test-part**."), ("user", "earlier question"), ("tool", "`run_python` x"),
              ("claude", "earlier answer"), ("note", "Continuing this conversation.")], str(pan.entries))
    newchat = [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0]
    newchat.click()
    check("local: New chat calls local_engine.clear(project)", stub.calls[-1] == ("clear", project), str(stub.calls))
    check("local: New chat empties the view and leaves the Claude Code session alone",
          pan.entries == [("note", "New conversation.")] and projects.get_session(project) == "claude-session-keep")
    stub.replay[:] = [("text", "half an answer")]          # a run that does not end
    pan.send("long job")
    spin(50)
    newchat.click()
    check("local: New chat while the model works stops it first, quietly",
          stub.calls[-2:] == [("stop",), ("clear", project)] and pan.entries == [("note", "New conversation.")]
          and pan.send_btn.text() == "Send", str(stub.calls[-3:]) + str(pan.entries))
    stub.replay[:] = [("text", "half an answer")]
    pan.send("long job")
    spin(50)
    pan._select("other-part", open_model=False)
    check("local: changing the project stops the run, quietly",
          ("stop",) in stub.calls[-3:] and pan.send_btn.text() == "Send" and pan.project == other
          and not kinds(pan, "error") and ("note", "Stopped.") not in pan.entries,
          str((stub.calls[-4:], pan.send_btn.text(), pan.project, pan.entries)))
    pan._select("test-part", open_model=False)
    check("local: ClaudeRun.start was never called in all of this", not starts)
    paths.find_tool = real_find_tool
    bridge_ok[0] = True

    # =====================================================================
    say("\n-- panel: settings change the source --")
    def settings(change):
        """Open the real dialog as the panel does, let `change` click in it, press OK."""
        def fake_exec(dlg):
            change(dlg)
            dlg._save()
            return dlg.result()
        SettingsDialog.exec = fake_exec
        try:
            pan._settings()
        finally:
            del SettingsDialog.exec
        spin(100)

    p.SetString("LocalModel", "")
    pan.check_setup()
    check("(setup) local without a model shows its message", len(setup_errors(pan)) == 1)
    settings(lambda d: d.local_model.setEditText("google/gemma-4-e4b"))
    check("after Settings > OK the check runs again: the stale message is gone",
          not setup_errors(pan) and pan.source.full == "Local model · google/gemma-4-e4b", pan.source.full)
    os.environ["PANEL_FAKE_LOGGED_IN"] = "0"
    n_claude = len(claude_calls())
    del stub.calls[:]
    settings(lambda d: d.account.click())
    wait_for(lambda: len(claude_calls()) > n_claude and setup_errors(pan), 5)
    auth = claude_calls()[n_claude:]
    check("switch to the account: the label follows, the Claude Code checks run",
          pan.source.full == "Claude account · sonnet" and pan.claude == fake_claude
          and [c["argv"] for c in auth] == [["auth", "status"]], pan.source.full + str([c["argv"] for c in auth]))
    check("`claude auth status` runs without the shell's key, token and endpoint",
          bool(auth) and not [k for k in auth[0]["env"] if k in (
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")], str(auth and auth[0]["env"]))
    check("account, not signed in: the old message", len(setup_errors(pan)) == 1
          and setup_errors(pan)[0].startswith("Claude Code is not signed in."), str(setup_errors(pan)))
    check("switch to the account: the view shows the Claude Code conversation, not the local one",
          ("history", project) not in stub.calls and pan.entries[0] == ("note", "Project **test-part**."))
    stub.past[project] = [("user", "local question"), ("claude", "local answer")]
    settings(lambda d: d.local.click())
    check("switch to local: 'not signed in' is gone, the local conversation is loaded",
          not setup_errors(pan) and ("user", "local question") in pan.entries
          and pan.source.full.startswith("Local model"), str(pan.entries))
    n_claude = len(claude_calls())
    settings(lambda d: (d.apikey.click(), d.api_key.drop.click()))
    spin(300)
    check("switch to API key without a key: says where to put it",
          setup_errors(pan) == ["No Anthropic API key is saved. Open Settings and paste your key."]
          and backends.api_key() == "", str(setup_errors(pan)))
    pan.send("hello")
    spin(50)
    check("API key without a key: nothing is sent, the message is shown once",
          not starts and len(setup_errors(pan)) == 1 and not kinds(pan, "user"))
    settings(lambda d: d.api_key.edit.setText("sk-ant-made-up-3"))
    spin(300)
    check("API key saved: ready; 'not signed in' is not checked and not shown",
          not setup_errors(pan) and len(claude_calls()) == n_claude
          and pan.source.full == "Anthropic API key · sonnet", str(setup_errors(pan)) + pan.source.full)
    os.environ["PANEL_FAKE_LOGGED_IN"] = "1"

    # =====================================================================
    say("\n-- panel: Claude Code sources (ClaudeRun.start replaced by a recorder) --")
    del stub.calls[:]
    shell = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

    def send_recorded(backend, text):
        p.SetString("Backend", backend)
        del starts[:]
        pan.check_setup()
        pan.send(text)
        if not starts:
            return None, None
        (claude, proj, prompt, session, env), _kw = starts[-1]
        pe = runner.claude_environment(claude, env)      # exactly what ClaudeRun.start gives the process
        pan.run.finished.emit({"is_error": False, "result": "ok"})
        return (claude, proj, prompt, session, env), pe

    args, pe = send_recorded("account", "make a flange")
    check("account: send() goes to ClaudeRun.start(claude, project, text, session, env)",
          args is not None and args[:4] == (fake_claude, project, "make a flange", "claude-session-keep"), str(args))
    check("account: LocalRun is not used", not [c for c in stub.calls if c[0] == "start"])
    check("account: the bridge token and FreeCAD's path are handed over",
          args[4].get("FREECAD_MCP_TOKEN") == "test-token" and "CADPROMPT_FREECADCMD" in args[4]
          and args[4].get("FREECAD_MCP_VERSION") == paths.FREECAD_MCP_VERSION, str(args[4]))
    check("account: ANTHROPIC_API_KEY is set in os.environ for this test",
          os.environ.get("ANTHROPIC_API_KEY") == "shell-key-must-not-leak"
          and QtCore.QProcessEnvironment.systemEnvironment().value("ANTHROPIC_API_KEY") == "shell-key-must-not-leak")
    check("account: the process environment has ANTHROPIC_API_KEY, _AUTH_TOKEN and _BASE_URL removed",
          not [k for k in shell if pe.contains(k)] and pe.value("FREECAD_MCP_TOKEN") == "test-token",
          str([k for k in shell if pe.contains(k)]))
    check("account: the user's other variables are still there",
          pe.value("HOME") == (os.environ.get("HOME") or "") and pe.value("PANEL_BUNDLE_OTHER") == "kept"
          and pe.value("PATH").split(os.pathsep)[0] == bindir, pe.value("PATH")[:300])
    inside = [k for k in pe.keys() if bundle in pe.value(k)]
    check("account: nothing that points into the FreeCAD bundle is handed on (PYTHONPATH, a certificate file, PATH)",
          not inside and not pe.contains("PYTHONPATH") and not pe.contains("PANEL_BUNDLE_CERT"), str(inside))
    check("account: FreeCAD's own PYTHONHOME is not handed on",
          not pe.contains("PYTHONHOME") or not real_roots, pe.value("PYTHONHOME"))

    args, pe = send_recorded("apikey", "make a shaft")
    check("apikey: also goes to ClaudeRun.start", args is not None and args[:3] == (fake_claude, project, "make a shaft"))
    check("apikey: the process environment has the saved key, not the shell's",
          pe.value("ANTHROPIC_API_KEY") == "sk-ant-made-up-3", pe.value("ANTHROPIC_API_KEY"))
    check("apikey: the shell's token and endpoint are removed",
          not pe.contains("ANTHROPIC_AUTH_TOKEN") and not pe.contains("ANTHROPIC_BASE_URL"))

    args, pe = send_recorded("custom", "make a pin")
    check("custom: goes to ClaudeRun.start with the endpoint from Settings and no API key",
          args is not None and pe.value("ANTHROPIC_BASE_URL") == "http://localhost:11434"
          and pe.value("ANTHROPIC_AUTH_TOKEN") == "endpoint-token" and pe.value("ANTHROPIC_MODEL") == "endpoint-model"
          and not pe.contains("ANTHROPIC_API_KEY"))
    check("custom: the old backend_env() still answers", runner.backend_env().get("ANTHROPIC_MODEL") == "endpoint-model")

    # the Claude Code result handling is as before
    p.SetString("Backend", "account")
    pan.check_setup()
    del starts[:]
    pan.send("make a flange")
    check("account: while it works: 'Claude is working…' and Stop",
          pan.status.text() == "Claude is working…" and pan.send_btn.text() == "Stop")
    pan.run.tool.emit("execute_code", "doc.recompute()")
    pan.run.tool_error.emit("execute_code")
    check("account: tool status and tool error name Claude",
          pan.status.text() == "Claude is working… (execute_code)"
          and pan.entries[-1] == ("error", "`execute_code` failed, Claude is looking at why"), str(pan.entries[-1]))
    pan.run.finished.emit({"is_error": False, "result": "ok", "session_id": "claude-session-new"})
    check("account: the session id of the result is stored, as before",
          projects.get_session(project) == "claude-session-new" and pan.send_btn.text() == "Send")
    pan.send("more")
    pan.run.finished.emit({"is_error": True, "result": "Invalid API key · Please run /login"})
    check("account: a login error still gets the sign-in hint",
          pan.entries[-1][0] == "error" and "run `claude`, and sign in" in pan.entries[-1][1], str(pan.entries[-1]))
    pan.send("more")
    pan.run.finished.emit({"is_error": False, "result": "", "permission_denials": [{"tool_name": "WebSearch"}]})
    check("account: a blocked web tool still points to Settings",
          pan.entries[-1] == ("error", "`WebSearch` was blocked: web access is off. "
                                       "Turn it on in Settings, then ask again."), str(pan.entries[-1]))
    check("account: tooltips and notes name Claude", pan.build_btn.toolTip() == "Claude builds the whole part"
          and pan.refs_btn.toolTip() == "Copy photos, PDFs or drawings into refs/ for Claude")
    mime = QtCore.QMimeData()
    mime.setText("https://example.com/datasheet.pdf")
    check("account: a pasted link becomes a reference, 'Tell Claude…'", pan.take_mime(mime)
          and pan.entries[-1][1].endswith("Tell Claude what to do with them.")
          and "example.com/datasheet.pdf" in open(os.path.join(project, "refs", "links.md")).read())
    [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0].click()
    check("account: New chat forgets the Claude Code session and does not clear the local conversation",
          projects.get_session(project) is None and ("clear", project) not in stub.calls)
    paths.in_flatpak = lambda: True
    check("account in a Flatpak: the old message, plus the way out",
          pan.check_setup() is False and len(setup_errors(pan)) == 1 and "Flatpak" in setup_errors(pan)[0]
          and "local model in Settings" in setup_errors(pan)[0], str(setup_errors(pan)))
    paths.in_flatpak = real_flatpak
    paths.find_tool = lambda name: None
    check("account without Claude Code: the install hint is still shown",
          pan.check_setup() is False and setup_errors(pan)[0].startswith("Claude Code is not installed."),
          str(setup_errors(pan)))
    paths.find_tool = real_find_tool
    pan.check_setup()
    spin(300)

    # =====================================================================
    say("\n-- panel: the real ClaudeRun with the pretend claude program --")
    runner.ClaudeRun.start = real_start

    def run_fake(backend, text):
        p.SetString("Backend", backend)
        pan.check_setup()
        spin(300)                                # lets `auth status` of the account finish
        n = len(claude_calls())
        pan.send(text)
        wait_for(lambda: pan.send_btn.text() == "Send" and len(claude_calls()) > n, 15)
        runs = [c for c in claude_calls()[n:] if c["argv"][:1] == ["-p"]]
        return runs[-1] if runs else {"argv": [], "env": {}, "stdin": "", "cwd": "", "path0": "", "path": []}

    p.SetBool("AllowWeb", True)
    rec = run_fake("account", "make a bracket")
    check("account: the program gets the prompt on stdin, in the project folder",
          rec["stdin"] == "make a bracket" and os.path.realpath(rec["cwd"]) == os.path.realpath(project), str(rec))
    check("account: the program is found in a folder with a space and a letter that is not ASCII",
          rec["path0"] == bindir and " " in bindir, f"{rec['path0']!r} {bindir!r}")
    check("account: the started program got nothing that points into the FreeCAD bundle",
          not [k for k, v in rec["env"].items() if bundle in v] and "PYTHONPATH" not in rec["env"]
          and "PYTHONHOME" not in rec["env"] and not [d for d in rec["path"] if d.startswith(bundle)]
          and rec["env"].get("PANEL_BUNDLE_OTHER") == "kept",
          str({k: v for k, v in rec["env"].items() if k.startswith(("PYTHON", "PANEL_"))}) + str(rec["path"][:3]))
    check("account: arguments as before (stream-json, model, web tools)",
          rec["argv"] == ["-p", "--output-format", "stream-json", "--verbose", "--model", "sonnet",
                          "--allowedTools", "WebSearch", "WebFetch"], str(rec["argv"]))
    check("account: the started program has no ANTHROPIC_API_KEY, _AUTH_TOKEN, _BASE_URL",
          not [k for k in shell if k in rec["env"]] and rec["env"].get("FREECAD_MCP_TOKEN") == "test-token",
          str(rec["env"]))
    check("account: answer, tool line and session id arrive in the panel",
          ("claude", "fake answer to: make a bracket") in pan.entries
          and ("tool", "`execute_code` doc.recompute()") in pan.entries
          and projects.get_session(project) == "fake-session-1", str(pan.entries[-3:]))
    rec = run_fake("apikey", "make a lever")
    check("apikey: the started program has the saved key, resumes the session",
          rec["env"].get("ANTHROPIC_API_KEY") == "sk-ant-made-up-3" and "ANTHROPIC_BASE_URL" not in rec["env"]
          and "ANTHROPIC_AUTH_TOKEN" not in rec["env"] and rec["argv"][4:6] == ["--resume", "fake-session-1"],
          str(rec))
    rec = run_fake("custom", "make a knob")
    check("custom: the started program has the endpoint and no API key, and no Claude model argument",
          rec["env"].get("ANTHROPIC_BASE_URL") == "http://localhost:11434" and "ANTHROPIC_API_KEY" not in rec["env"]
          and "--model" not in rec["argv"], str(rec))
    p.SetBool("AllowWeb", False)

    # a session id that Claude Code does not have any more (transcript deleted, other computer)
    projects.set_session(project, "gone-session")
    p.SetString("Backend", "account")
    pan.check_setup()
    spin(300)
    n = len(claude_calls())
    pan.send("make a washer")
    wait_for(lambda: pan.send_btn.text() == "Send", 15)
    runs = [c["argv"] for c in claude_calls()[n:] if c["argv"][:1] == ["-p"]]
    check("account, lost conversation: asked again without --resume",
          len(runs) == 2 and runs[0][4:6] == ["--resume", "gone-session"] and "--resume" not in runs[1], str(runs))
    check("account, lost conversation: the note and then the answer of the new run are shown",
          pan.entries[-4:-1] == [("user", "make a washer"),
                                 ("note", "The earlier conversation could not be found; starting a new one."),
                                 ("claude", "fake answer to: make a washer")], str(pan.entries[-4:]))
    check("account, lost conversation: the panel is idle again and the new session is stored",
          pan.send_btn.text() == "Send" and pan.status.text() == "" and not pan.run.running()
          and projects.get_session(project) == "fake-session-1",
          str((pan.send_btn.text(), pan.status.text(), projects.get_session(project))))

    # a prompt with letters that are not ASCII arrives as it was typed
    rec = run_fake("account", "make a Ø80 flange, 4 × M8, größer")
    check("account: a prompt with Ø, × and ö arrives as typed, and so does the answer",
          rec["stdin"] == "make a Ø80 flange, 4 × M8, größer"
          and ("claude", "fake answer to: make a Ø80 flange, 4 × M8, größer") in pan.entries,
          repr(rec["stdin"]) + str(pan.entries[-2:]))

    # Stop ends the program, and what the program started (Windows: the program behind claude.cmd)
    def alive(pid):
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259                                             # STILL_ACTIVE
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    n = len(claude_calls())
    pan.send("slow: make a housing")
    started = wait_for(lambda: [c for c in claude_calls()[n:] if c["stdin"].startswith("slow")], 15)
    pid = ([c["pid"] for c in claude_calls()[n:] if c["stdin"].startswith("slow")] or [0])[0]
    check("account: (setup) a run that takes long is under way, the button says Stop",
          bool(started) and pid and alive(pid) and pan.send_btn.text() == "Stop", f"{pid} {pan.send_btn.text()}")
    pan.send_btn.click()                     # Stop
    stopped = wait_for(lambda: pan.send_btn.text() == "Send" and not alive(pid), 15)
    check("account: Stop ends the program that does the work, and the panel says 'Stopped.'",
          stopped and pan.entries[-1] == ("note", "Stopped."), f"alive={alive(pid) if pid else None} {pan.entries[-2:]}")

    # the two engines side by side
    p.SetString("Backend", "local")
    pan.check_setup()
    n = len(claude_calls())
    del stub.calls[:]
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    pan.send("back to local")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    spin(200)
    check("back on local: the stub engine gets the message, claude is not started",
          stub.calls[-1][:3] == ("start", project, "back to local") and len(claude_calls()) == n
          and projects.get_session(project) == "fake-session-1")

    check("no exception in any Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))
    for pan in panels:
        pan.deleteLater()


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
